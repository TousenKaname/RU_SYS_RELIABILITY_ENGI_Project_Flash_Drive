"""exFAT on-disk records at the start of a volume, read raw and checked.

Run 1 found that the ABLAZE 8 GB drives lost the first 256 KiB of their volumes (boot
area excepted): allocation bitmap, up-case table, root directory and the identity file
next to them came back with 10-15 % of their bits flipped. Three of these records can be
checked without knowing what the file system was doing at that moment:

* the **boot region** (sectors 0-11) carries its own checksum in sector 11, and the
  only fields Windows changes later (VolumeFlags, PercentInUse) are excluded from it;
* the **up-case table** is written once by the format and carries a checksum in its
  root-directory entry (0xE619D30D for the table Windows writes);
* every in-use **directory entry set** carries a 16-bit SetChecksum.

The checks follow Microsoft's exFAT specification (sections 3.1, 3.4, 6.3, 7.2). They
only read; nothing is ever written to a drive.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

END_OF_CHAIN = 0xFFFFFFFF
BAD_CLUSTER = 0xFFFFFFF7


class ExfatError(ValueError):
    """The bytes are not a valid exFAT structure."""


class Reader(Protocol):
    def read(self, offset: int, length: int) -> bytes: ...


def checksum32(data: bytes, skip: frozenset[int] = frozenset()) -> int:
    """The rotate-right-and-add checksum of the boot region and the up-case table."""
    c = 0
    for i, b in enumerate(data):
        if i in skip:
            continue
        c = (((c >> 1) | ((c & 1) << 31)) + b) & 0xFFFFFFFF
    return c


def set_checksum(data: bytes) -> int:
    """16-bit checksum of a directory entry set (bytes 2-3 of the first entry skipped)."""
    c = 0
    for i, b in enumerate(data):
        if i in (2, 3):
            continue
        c = ((((c & 1) << 15) | (c >> 1)) + b) & 0xFFFF
    return c


BOOT_SKIP = frozenset({106, 107, 112})  # VolumeFlags (2 bytes) and PercentInUse


@dataclass(frozen=True)
class Boot:
    bytes_per_sector: int
    sectors_per_cluster: int
    fat_offset: int        # sectors
    fat_length: int        # sectors
    heap_offset: int       # sectors
    cluster_count: int
    root_cluster: int
    vsn: int
    revision: int
    volume_flags: int
    percent_in_use: int
    number_of_fats: int
    volume_length: int     # sectors

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def dirty(self) -> bool:
        return bool(self.volume_flags & 0x0002)

    def cluster_offset(self, cluster: int) -> int:
        if not 2 <= cluster < self.cluster_count + 2:
            raise ExfatError(f"cluster {cluster} outside the heap")
        return (self.heap_offset + (cluster - 2) * self.sectors_per_cluster) * self.bytes_per_sector

    def geometry(self) -> dict[str, int]:
        return {k: v for k, v in asdict(self).items()
                if k not in ("volume_flags", "percent_in_use")}


def parse_boot(sector0: bytes) -> Boot:
    if len(sector0) < 512:
        raise ExfatError("boot sector shorter than 512 bytes")
    if sector0[3:11] != b"EXFAT   ":
        raise ExfatError("not an exFAT boot sector")
    if sector0[510:512] != b"\x55\xaa":
        raise ExfatError("boot signature missing")
    if any(sector0[11:64]):
        raise ExfatError("MustBeZero field is not zero")
    volume_length, = struct.unpack_from("<Q", sector0, 72)
    fat_offset, fat_length, heap_offset, cluster_count, root = struct.unpack_from("<IIIII", sector0, 80)
    vsn, = struct.unpack_from("<I", sector0, 100)
    revision, flags = struct.unpack_from("<HH", sector0, 104)
    bps_shift, spc_shift, nfats = sector0[108], sector0[109], sector0[110]
    if not 9 <= bps_shift <= 12 or spc_shift > 25 - bps_shift:
        raise ExfatError("implausible sector or cluster size")
    if not 2 <= root < cluster_count + 2:
        raise ExfatError("root directory cluster outside the heap")
    return Boot(1 << bps_shift, 1 << spc_shift, fat_offset, fat_length, heap_offset, cluster_count,
                root, vsn, revision, flags, sector0[112], nfats, volume_length)


def boot_region_checksum(region: bytes, bytes_per_sector: int) -> dict[str, Any]:
    """Checksum of sectors 0-10 against the values stored in sector 11."""
    need = 12 * bytes_per_sector
    if len(region) < need:
        raise ExfatError("boot region shorter than 12 sectors")
    computed = checksum32(region[: 11 * bytes_per_sector], BOOT_SKIP)
    sector11 = region[11 * bytes_per_sector: need]
    stored = struct.unpack_from(f"<{bytes_per_sector // 4}I", sector11)
    return {"computed": computed, "stored": stored[0], "consistent": len(set(stored)) == 1,
            "ok": len(set(stored)) == 1 and stored[0] == computed}


# -- directories --------------------------------------------------------------------------------
@dataclass
class DirEntry:
    kind: str                  # bitmap, upcase, label, guid, file, dir, other
    offset: int                # byte offset of the primary entry in the directory data
    entry_type: int
    name: str = ""
    first_cluster: int = 0
    data_length: int = 0
    no_fat_chain: bool = False
    table_checksum: int | None = None
    checksum_ok: bool = True


@dataclass
class DirParse:
    entries: list[DirEntry] = field(default_factory=list)
    bad_sets: int = 0          # in-use entry sets whose SetChecksum does not match
    unknown: int = 0           # in-use entries of a type that cannot appear here
    end_found: bool = False

    @property
    def ok(self) -> bool:
        return self.bad_sets == 0 and self.unknown == 0

    def find(self, name: str) -> DirEntry | None:
        lname = name.lower()
        return next((e for e in self.entries if e.kind in ("file", "dir") and e.name.lower() == lname),
                    None)

    def first(self, kind: str) -> DirEntry | None:
        return next((e for e in self.entries if e.kind == kind), None)


# In-use entry types that may follow a file entry (stream extension, file name, vendor).
_SECONDARY = {0xC0, 0xC1, 0xE0, 0xE1}
_BENIGN_PRIMARY = {0xA1, 0xA2}  # TexFAT padding, Windows CE access control


def parse_directory(data: bytes) -> DirParse:
    out = DirParse()
    n = len(data) // 32
    i = 0
    while i < n:
        off = i * 32
        e = data[off: off + 32]
        t = e[0]
        if t == 0x00:
            out.end_found = True
            break
        if not t & 0x80:  # not in use (deleted entry or unused)
            i += 1
            continue
        if t == 0x81:
            fc, ln = struct.unpack_from("<IQ", e, 20)
            out.entries.append(DirEntry("bitmap", off, t, first_cluster=fc, data_length=ln))
            i += 1
        elif t == 0x82:
            tcs, = struct.unpack_from("<I", e, 4)
            fc, ln = struct.unpack_from("<IQ", e, 20)
            out.entries.append(DirEntry("upcase", off, t, first_cluster=fc, data_length=ln,
                                        table_checksum=tcs))
            i += 1
        elif t == 0x83:
            count = min(e[1], 11)
            out.entries.append(DirEntry("label", off, t,
                                        name=e[2: 2 + 2 * count].decode("utf-16-le", "replace")))
            i += 1
        elif t in (0x85, 0xA0):
            sc = e[1]
            # A file set has a stream extension and 1-17 name entries; a volume GUID none.
            valid_count = 2 <= sc <= 18 if t == 0x85 else sc == 0
            if not valid_count or i + sc >= n:
                out.bad_sets += 1
                i += 1
                continue
            set_bytes = data[off: off + 32 * (1 + sc)]
            stored, = struct.unpack_from("<H", e, 2)
            ok = len(set_bytes) == 32 * (1 + sc) and set_checksum(set_bytes) == stored
            if t == 0xA0:
                out.entries.append(DirEntry("guid", off, t, checksum_ok=ok))
            else:
                attrs, = struct.unpack_from("<H", e, 4)
                entry = DirEntry("dir" if attrs & 0x10 else "file", off, t, checksum_ok=ok)
                if ok and sc >= 2 and set_bytes[32] == 0xC0:
                    stream = set_bytes[32:64]
                    entry.no_fat_chain = bool(stream[1] & 0x02)
                    name_len = stream[3]
                    entry.first_cluster, entry.data_length = struct.unpack_from("<IQ", stream, 20)
                    chars = b"".join(set_bytes[32 * k + 2: 32 * k + 32] for k in range(2, 1 + sc)
                                     if set_bytes[32 * k] == 0xC1)
                    entry.name = chars[: 2 * name_len].decode("utf-16-le", "replace")
                out.entries.append(entry)
            if not ok:
                out.bad_sets += 1
            i += 1 + sc
        elif t in _BENIGN_PRIMARY:
            i += 1
        else:
            # A stray secondary entry or an unknown primary type in use: garbage.
            out.unknown += 1
            i += 1
    return out


# -- reading clusters ---------------------------------------------------------------------------
class VolumeReader:
    def __init__(self, raw: Reader, boot: Boot | None = None) -> None:
        self.raw = raw
        self.boot = boot

    def boot_region(self, bytes_per_sector: int = 512) -> bytes:
        return self.raw.read(0, 24 * bytes_per_sector)

    def cluster(self, number: int) -> bytes:
        assert self.boot is not None
        return self.raw.read(self.boot.cluster_offset(number), self.boot.cluster_size)

    def fat_entry(self, number: int) -> int:
        assert self.boot is not None
        offset = self.boot.fat_offset * self.boot.bytes_per_sector + 4 * number
        value, = struct.unpack("<I", self.raw.read(offset, 4))
        return value

    def chain(self, first: int, length: int | None = None, *, no_fat_chain: bool = False,
              max_clusters: int = 64) -> bytes:
        """Data of a cluster chain (``length`` bytes, or up to the end of the FAT chain)."""
        assert self.boot is not None
        size = self.boot.cluster_size
        wanted = -(-length // size) if length else max_clusters
        wanted = min(wanted, max_clusters)
        parts, cluster = [], first
        for _ in range(wanted):
            parts.append(self.cluster(cluster))
            if no_fat_chain or length and len(parts) * size >= length:
                cluster += 1
                continue
            nxt = self.fat_entry(cluster)
            if nxt == END_OF_CHAIN:
                break
            if nxt in (0, 1, BAD_CLUSTER) or not 2 <= nxt < self.boot.cluster_count + 2:
                if length:  # contiguous allocation without FAT entries
                    cluster += 1
                    continue
                break
            cluster = nxt
        data = b"".join(parts)
        return data[:length] if length else data


# -- baseline at intake, and the periodic check -------------------------------------------------
def baseline(raw: Reader, *, identity_name: str, sentinel_name: str) -> dict[str, Any]:
    """Record what the healthy volume looks like right after intake.

    Raises ExfatError when the volume does not pass its own checks, in which case the
    raw checks are switched off for that drive (they must never produce false failures).
    """
    sector0 = raw.read(0, 512)
    boot = parse_boot(sector0)
    region = raw.read(0, 24 * boot.bytes_per_sector)
    main = boot_region_checksum(region[: 12 * boot.bytes_per_sector], boot.bytes_per_sector)
    if not main["ok"]:
        raise ExfatError(f"boot checksum mismatch at intake ({main})")
    reader = VolumeReader(raw, boot)
    root = parse_directory(reader.chain(boot.root_cluster))
    if not root.ok:
        raise ExfatError(f"root directory does not parse at intake ({root.bad_sets} bad sets, "
                         f"{root.unknown} unknown entries)")
    upcase, bitmap = root.first("upcase"), root.first("bitmap")
    if upcase is None or bitmap is None:
        raise ExfatError("root directory lacks the bitmap or up-case entry")
    table = reader.chain(upcase.first_cluster, upcase.data_length)
    computed = checksum32(table)
    if computed != upcase.table_checksum:
        raise ExfatError(f"up-case checksum mismatch at intake ({computed:#x} vs "
                         f"{upcase.table_checksum:#x})")
    files = {}
    for name in (identity_name, sentinel_name):
        entry = root.find(name)
        if entry is None:
            raise ExfatError(f"{name} not found in the root directory at intake")
        files[name] = {"first_cluster": entry.first_cluster, "data_length": entry.data_length}
    return {
        "geometry": boot.geometry(), "cluster_size": boot.cluster_size,
        "boot_checksum": main["computed"],
        "upcase": {"first_cluster": upcase.first_cluster, "data_length": upcase.data_length,
                   "checksum": upcase.table_checksum},
        "bitmap": {"first_cluster": bitmap.first_cluster, "data_length": bitmap.data_length},
        "files": files,
        "root_entries": [e.name or e.kind for e in root.entries],
    }


def check(raw: Reader, base: dict[str, Any]) -> dict[str, Any]:
    """Compare the volume with its intake baseline. Never raises on bad data.

    Returns ``{"boot": {...}, "upcase": {...}, "rootdir": {...}, "dirty": bool, ...}``
    where each component has ``ok`` (True, False) and a short ``detail``.
    """
    result: dict[str, Any] = {}
    geometry = base["geometry"]
    bps = int(geometry["bytes_per_sector"])
    try:
        region = raw.read(0, 12 * bps)
        boot = parse_boot(region[:512])
        cs = boot_region_checksum(region, bps)
        same = boot.geometry() == geometry
        ok = cs["ok"] and cs["computed"] == base["boot_checksum"] and same
        result["boot"] = {"ok": ok, "detail": "" if ok else
                          f"computed {cs['computed']:#010x}, stored {cs['stored']:#010x}, "
                          f"baseline {base['boot_checksum']:#010x}, geometry same: {same}"}
        result["dirty"] = boot.dirty
        result["percent_in_use"] = boot.percent_in_use
    except ExfatError as exc:
        result["boot"] = {"ok": False, "detail": f"boot sector unreadable as exFAT: {exc}"}
        boot = Boot(**geometry, volume_flags=0, percent_in_use=0)
    reader = VolumeReader(raw, boot)
    up = base["upcase"]
    try:
        table = reader.chain(up["first_cluster"], up["data_length"])
        computed = checksum32(table)
        ok = computed == up["checksum"]
        result["upcase"] = {"ok": ok, "detail": "" if ok else
                            f"checksum {computed:#010x} instead of {up['checksum']:#010x}"}
    except ExfatError as exc:
        result["upcase"] = {"ok": False, "detail": str(exc)}
    try:
        root = parse_directory(reader.chain(boot.root_cluster))
        problems = []
        if root.bad_sets or root.unknown:
            problems.append(f"{root.bad_sets} bad entry sets, {root.unknown} stray entries")
        upcase, bitmap = root.first("upcase"), root.first("bitmap")
        if upcase is None or (upcase.first_cluster, upcase.table_checksum) != (up["first_cluster"], up["checksum"]):
            problems.append("up-case entry missing or changed")
        if bitmap is None or bitmap.first_cluster != base["bitmap"]["first_cluster"]:
            problems.append("bitmap entry missing or changed")
        for name, meta in base["files"].items():
            entry = root.find(name)
            if entry is None:
                problems.append(f"{name} entry missing")
            elif (entry.first_cluster, entry.data_length) != (meta["first_cluster"], meta["data_length"]):
                problems.append(f"{name} entry changed")
        result["rootdir"] = {"ok": not problems, "detail": "; ".join(problems)}
    except ExfatError as exc:
        result["rootdir"] = {"ok": False, "detail": str(exc)}
    return result
