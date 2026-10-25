"""Raw reads of the test drives after the test: what the failed drives returned.

    python scripts/forensics.py capture configs/campaign.yaml --out data/fall2026/forensics
        [--dir A16-01:FLASHREL-DATA/c000009/d0342 ...]
    python scripts/forensics.py figure configs/campaign.yaml --data data/fall2026/forensics
        --out <report>/results

``capture`` opens every test volume that Windows has mounted (found by its volume label)
with a read-only handle and writes nothing to it. For every drive it saves the boot sector
and the first clusters of the data area, where exFAT keeps its own records (allocation
bitmap, up-case table, root directory, Windows' folder and its two files, the identity
file, FLASHREL-DATA), reads the identity file around the host's cache, saves the directory
cluster of every folder named with ``--dir``, and, for every drive that failed, compares
the stale test data after those first clusters with the regenerated payload. The bits of
the first clusters are checked where their content is known: the up-case table (the same
on every volume formatted by Windows), the identity file (rebuilt from the intake record),
and the bytes that are zero on every intact volume of the same size. ``figure`` draws
``fig_forensics`` and writes ``forensics.tex`` with the numbers the report quotes.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import struct
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

ENTRY = 32
ROLES = ["allocation bitmap", "up-case table", "root directory", "Windows folder",
         "Windows file", "Windows file", "identity file", "FLASHREL-DATA"]
SHORT_ROLES = ["bitmap", "up-case\ntable", "root\nfolder", "Windows\nfolder", "Windows\nfile",
               "Windows\nfile", "identity\nfile", "test\nfolder"]
IDENTITY_ORDER = ["drive_id", "enrolled_at", "host", "campaign", "total_bytes", "filesystem"]
DATA_SCAN = 4096          # clusters of stale test data compared after the first clusters
HEADER = struct.Struct("<6sHQIIQ")

# -- read-only volume access (Windows) -----------------------------------------------------
k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if k32 is not None:
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k32.ReadFile.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    k32.SetFilePointerEx.argtypes = (wintypes.HANDLE, ctypes.c_longlong,
                                     ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD)
    k32.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        wintypes.DWORD)
INVALID = wintypes.HANDLE(-1).value
GENERIC_READ, SHARE_RW, OPEN_EXISTING = 0x80000000, 3, 3
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073


def volumes_by_label() -> dict[str, str]:
    """Volume label -> drive letter for every mounted volume (labels as Windows holds them)."""
    k32.SetErrorMode(1)  # no "insert a disk" dialog for an empty slot
    out = {}
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        name, fs = ctypes.create_unicode_buffer(261), ctypes.create_unicode_buffer(261)
        if k32.GetVolumeInformationW(f"{letter}:\\", name, 261, None, None, None, fs, 261):
            out[name.value] = letter
    return out


class Volume:
    """A mounted exFAT volume opened for reading only."""

    def __init__(self, letter: str):
        self.letter = letter
        self.h = k32.CreateFileW(f"\\\\.\\{letter}:", GENERIC_READ, SHARE_RW, None,
                                 OPEN_EXISTING, 0, None)
        if self.h == INVALID:
            raise OSError(ctypes.get_last_error(), f"cannot open volume {letter}:")
        self.boot = self.read(0, 512)
        if self.boot[3:11] != b"EXFAT   ":
            raise ValueError(f"{letter}: is not exFAT")
        self.heap = struct.unpack_from("<I", self.boot, 88)[0] << self.boot[108]
        self.count = struct.unpack_from("<I", self.boot, 92)[0]
        self.root = struct.unpack_from("<I", self.boot, 96)[0]
        self.cluster_size = 1 << (self.boot[108] + self.boot[109])

    def read(self, offset: int, size: int) -> bytes:
        if not k32.SetFilePointerEx(self.h, offset, None, 0):
            raise OSError(ctypes.get_last_error(), "seek")
        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD()
        if not k32.ReadFile(self.h, buf, size, ctypes.byref(got), None):
            raise OSError(ctypes.get_last_error(), "read")
        return buf.raw[:got.value]

    def clusters(self, first: int, n: int = 1) -> bytes:
        """``n`` clusters from exFAT cluster ``first`` (the data area starts at cluster 2)."""
        return self.read(self.heap + (first - 2) * self.cluster_size, n * self.cluster_size)

    def close(self) -> None:
        k32.CloseHandle(self.h)


def first_cluster_of(path: Path) -> int:
    """exFAT cluster number of a file's or folder's first cluster (no data is read)."""
    h = k32.CreateFileW(str(path), 0x80, 7, None, OPEN_EXISTING, 0x02000000, None)
    if h == INVALID:
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    try:
        start, out, got = ctypes.c_longlong(0), ctypes.create_string_buffer(64), wintypes.DWORD()
        k32.DeviceIoControl(h, FSCTL_GET_RETRIEVAL_POINTERS, ctypes.byref(start), 8, out, 64,
                            ctypes.byref(got), None)
        lcn = struct.unpack_from("<q", out.raw, 24)[0]  # first extent: next VCN, then LCN
        return lcn + 2  # Windows counts clusters from the start of the data area
    finally:
        k32.CloseHandle(h)


# -- what the first clusters should hold -----------------------------------------------------
def identity_bytes(intake: dict) -> bytes:
    """The identity file as written at intake (intake.json keeps its keys sorted)."""
    ident = intake["identity"]
    return json.dumps({k: ident[k] for k in IDENTITY_ORDER}, indent=2).encode("utf-8")


def bits(a: bytes, b: bytes) -> int:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).bit_count()


def first_cluster_roles(root: int) -> list[tuple[int, str]]:
    """(cluster, role) of the first clusters; the bitmap ends right before the up-case table."""
    return [(root - 2 + i, role) for i, role in enumerate(ROLES)]


def check_first_clusters(raw: dict[str, bytes], meta: dict[str, dict],
                         intakes: dict[str, dict]) -> dict[str, list[dict]]:
    """Wrong bits per first cluster, where the content is known (see the module docstring)."""
    out = {}
    for d, blob in raw.items():
        m = meta[d]
        cs, root = m["cluster_size"], m["root_cluster"]
        # intact volumes with the same layout serve as references
        refs = [raw[r] for r in raw if r != d and meta[r]["root_cluster"] == root
                and meta[r]["cluster_size"] == cs and meta[r]["identity"]["wrong_bits"] == 0
                and meta[r]["root_parses"]]
        rows = []
        for i, (cluster, role) in enumerate(first_cluster_roles(root)):
            got = blob[i * cs:(i + 1) * cs]
            if role == "up-case table" and refs:
                want, mask = refs[0][i * cs:(i + 1) * cs], None
            elif role == "identity file":
                ident = identity_bytes(intakes[d])
                want, mask = ident + bytes(cs - len(ident)), None
            elif refs:  # bytes that are zero on every reference must be zero here
                want = bytes(cs)
                zero = bytes(0xFF if all(r[i * cs + j] == 0 for r in refs) else 0
                             for j in range(cs))
                if role == "allocation bitmap":  # only past the end of the bitmap
                    used = (m["cluster_count"] + 7) // 8 - (cluster - 2) * cs
                    zero = bytes(min(max(used, 0), cs)) + zero[max(used, 0):]
                elif m["root_parses"]:  # an intact folder: past its own last entry
                    end = next((j for j in range(0, cs, ENTRY) if got[j] == 0), cs)
                    zero = bytes(end) + zero[end:]
                mask = zero
            else:
                rows.append({"cluster": cluster, "role": role, "known_bits": 0, "wrong_bits": 0})
                continue
            if mask is None:
                known, wrong = 8 * cs, bits(got, want)
            else:
                known = 8 * sum(1 for x in mask if x)
                wrong = sum(bin(g & k).count("1") for g, k in zip(got, mask))
            rows.append({"cluster": cluster, "role": role, "known_bits": known, "wrong_bits": wrong})
        out[d] = rows
    return out


# -- directory clusters ----------------------------------------------------------------------
def directory_map(data: bytes) -> list[dict]:
    """Runs of the 32-byte entries of a directory cluster of fNNNNNN.bin files.

    Kinds: ``deleted`` (entry sets deleted normally), ``in use`` (a well-formed entry or entry
    set still marked in use), ``random`` (anything else), ``unused`` (zero bytes).
    """
    n = len(data) // ENTRY
    e = [data[ENTRY * i:ENTRY * (i + 1)] for i in range(n)]

    def file_set(i):
        if i + 2 >= n or e[i][0] not in (0x85, 0x05) or e[i][1] != 2:
            return None
        if e[i + 1][0] not in (0xC0, 0x40) or e[i + 2][0] not in (0xC1, 0x41):
            return None
        name = e[i + 2][2:32].decode("utf-16-le", "replace").rstrip("\x00")
        if e[i + 1][3] != len(name) or not (name.startswith("f") and name.endswith(".bin")):
            return None
        return ("deleted" if e[i][0] == 0x05 else "in use"), name

    def test_name(entry: bytes) -> bool:
        name = entry[2:32].decode("utf-16-le", "replace").rstrip("\x00")
        return len(name) == 11 and name[0] == "f" and name[1:7].isdigit() and name[7:] == ".bin"

    def orphan(i: int) -> bool:
        """A real entry still marked in use whose set was cut by the random bytes."""
        t = e[i][0]
        if t == 0x85:  # file entry: two secondaries, ordinary attributes
            return e[i][1] == 2 and struct.unpack_from("<H", e[i], 4)[0] & ~0x27 == 0
        if t == 0xC1:
            return test_name(e[i])
        if t == 0xC0:
            return i + 1 < n and e[i + 1][0] == 0xC1 and test_name(e[i + 1]) and e[i][3] == 11
        return False

    items, i = [], 0
    while i < n:
        fs = file_set(i)
        if fs:
            items.append([i, i + 3, fs[0], fs[1], fs[1]])
            i += 3
            continue
        kind = "unused" if e[i] == bytes(ENTRY) else ("in use" if orphan(i) else "random")
        items.append([i, i + 1, kind, "", ""])
        i += 1
    runs = []
    for a, b, kind, first, last in items:
        if runs and runs[-1]["kind"] == kind:
            runs[-1]["end"] = ENTRY * b
            runs[-1]["last"] = last or runs[-1]["last"]
            continue
        runs.append({"start": ENTRY * a, "end": ENTRY * b, "kind": kind, "first": first,
                     "last": last})
    return runs


# -- stale test data after the first clusters ------------------------------------------------
def payload_index(campaign, drive_id: str) -> dict:
    """(cycle, file index) -> (key, size) for every cycle the drive logged.

    A cycle logged without its plan (its worker was stopped) is rebuilt from the free space
    at its start, the free space after the previous cycle; the content keys then check it.
    """
    from flashrel.payload import fill_budget, plan_cycle
    from flashrel.runner import MIN_PLANNING_CLUSTER
    index, free = {}, None
    path = Path(campaign.run_dir) / drive_id / "cycles.csv"
    for row in csv.DictReader(open(path, encoding="utf-8")):
        cycle = int(row["cycle"])
        before = int(row["free_before"]) if row["free_before"] else free
        if before is None:
            continue
        plan = plan_cycle(seed=campaign.seed, drive_id=drive_id, cycle=cycle,
                          workload=campaign.workload_for_cycle(cycle),
                          budget_bytes=fill_budget(before, campaign.cycle.fill_fraction,
                                                   campaign.cycle.reserve_bytes),
                          cluster_size=max(32768, MIN_PLANNING_CLUSTER),
                          chunk_size=campaign.cycle.chunk_size)
        for f in plan.files:
            index[(cycle, f.index)] = (f.key, f.size)
        free = int(row["free_after"]) if row["free_after"] else before
    return index


def scan_stale_data(vol: Volume, first: int, n: int, index: dict, chunk_size: int) -> dict:
    """Compare every chunk with a valid header in clusters first..first+n-1 with its payload."""
    cs = vol.cluster_size
    raw = b"".join(vol.clusters(c, min(32, first + n - c)) for c in range(first, first + n, 32))
    covered = bytearray(n)
    chunks = checked = wrong = with_errors = foreign = 0
    for k in range(n):
        if covered[k]:
            continue
        off = k * cs
        magic, ver, key, cyc, idx, ci = HEADER.unpack_from(raw, off)
        if magic != b"FLSHRL" or ver != 1:
            continue
        ks = index.get((cyc, idx))
        if ks is None or ks[0] != key:
            foreign += 1
            continue
        length = min(chunk_size, ks[1] - ci * chunk_size)
        if length <= 0:
            foreign += 1
            continue
        seed = key.to_bytes(8, "little") + ci.to_bytes(8, "little")
        want = (HEADER.pack(b"FLSHRL", 1, key, cyc, idx, ci)
                + hashlib.shake_128(seed).digest(max(0, length - 32)))[:length]
        got = raw[off:off + length]
        want = want[:len(got)]
        w = bits(got, want)
        chunks += 1
        checked += len(got)
        wrong += w
        with_errors += 1 if w else 0
        for j in range(k, min(n, k + -(-len(got) // cs))):
            covered[j] = 1
    return {"first_cluster": first, "clusters": n, "chunks": chunks, "bytes_checked": checked,
            "wrong_bits": wrong, "chunks_with_errors": with_errors,
            "headers_of_unknown_files": foreign, "clusters_not_covered": n - sum(covered)}


# -- capture ---------------------------------------------------------------------------------
def capture(args) -> int:
    from flashrel.config import load_campaign
    from flashrel.recorder import DriveLog
    from flashrel.system.directio import read_chunks
    campaign = load_campaign(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    letters = volumes_by_label()
    dirs: dict[str, list[str]] = {}
    for spec in args.dir or []:
        d, rel = spec.split(":", 1)
        dirs.setdefault(d, []).append(rel)
    meta, raw, intakes = {}, {}, {}
    for d in campaign.assignments:
        log = DriveLog(campaign.run_dir, d)
        intakes[d] = json.loads(log.intake_path.read_text())
        letter = letters.get(d)
        if letter is None:
            print(f"{d}: not mounted; skipped")
            continue
        vol = Volume(letter)
        try:
            cs = vol.cluster_size
            first = vol.clusters(vol.root - 2, len(ROLES))
            (out / f"{d}_boot.bin").write_bytes(vol.boot)
            (out / f"{d}_first_clusters_{vol.root - 2}-{vol.root - 3 + len(ROLES)}.bin"
             ).write_bytes(first)
            raw[d] = first
            root_dir = first[2 * cs:3 * cs]
            want_id = identity_bytes(intakes[d])
            try:
                got_id = b"".join(read_chunks(Path(f"{letter}:\\FLASHREL-ID.json"), 1 << 16,
                                              uncached=True))[:len(want_id)]
            except OSError as exc:
                got_id = b""
                print(f"{d}: identity file unreadable: {exc}")
            (out / f"{d}_identity.bin").write_bytes(got_id)
            pairs = list(zip(want_id, got_id))
            meta[d] = {
                "letter": letter, "status": log.load_state().status.value,
                "cluster_size": cs, "cluster_count": vol.count, "root_cluster": vol.root,
                "heap_offset": vol.heap,
                # an intact root directory starts with the label, bitmap and up-case entries
                "root_parses": {root_dir[0], root_dir[ENTRY], root_dir[2 * ENTRY]}
                == {0x81, 0x82, 0x83},
                "identity": {"bytes": len(want_id), "read": len(got_id),
                             "bad_bytes": sum(a != b for a, b in pairs) + len(want_id) - len(pairs),
                             "wrong_bits": bits(want_id[:len(got_id)], got_id)
                             + 8 * (len(want_id) - len(got_id)),
                             "one_to_zero": sum(bin(a & ~b & 0xFF).count("1") for a, b in pairs),
                             "zero_to_one": sum(bin(~a & b & 0xFF).count("1") for a, b in pairs)},
                "dirs": {},
            }
            for rel in dirs.get(d, []):
                cluster = first_cluster_of(Path(f"{letter}:\\") / rel)
                data = vol.clusters(cluster)
                name = rel.replace("/", "_").replace("\\", "_")
                (out / f"{d}_dir_{name}.bin").write_bytes(data)
                meta[d]["dirs"][rel] = {"cluster": cluster, "runs": directory_map(data)}
            if meta[d]["status"] in ("attention", "failed"):
                meta[d]["stale_data"] = scan_stale_data(
                    vol, vol.root - 2 + len(ROLES), DATA_SCAN, payload_index(campaign, d),
                    campaign.cycle.chunk_size)
        finally:
            vol.close()
        print(f"{d} {letter}: identity {meta[d]['identity']['wrong_bits']} wrong bits; "
              f"root folder parses: {meta[d]['root_parses']}"
              + (f"; stale data {meta[d]['stale_data']['bytes_checked'] / 2**20:.1f} MiB, "
                 f"{meta[d]['stale_data']['wrong_bits']} wrong bits" if "stale_data" in meta[d]
                 else ""))
    checks = check_first_clusters(raw, meta, intakes)
    for d, rows in checks.items():
        meta[d]["first_clusters"] = rows
        known = sum(r["known_bits"] for r in rows)
        wrong = sum(r["wrong_bits"] for r in rows)
        meta[d]["first_clusters_flip"] = wrong / known if known else None
        print(f"{d}: first clusters {wrong} wrong of {known} known bits "
              f"({100 * wrong / known if known else float('nan'):.2f} %)")
    summary = {"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "note": "read-only raw reads; nothing was written to any drive",
               "drives": meta}
    (out / "forensics.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"wrote {out / 'forensics.json'}")
    return 0


# -- figure ----------------------------------------------------------------------------------
def figure(args) -> int:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from flashrel.viz import style
    style.apply()
    data = json.loads((Path(args.data) / "forensics.json").read_text(encoding="utf-8"))
    drives = data["drives"]
    out = Path(args.out)
    colors = {"deleted": "#C9D3CF", "random": style.FAIL, "in use": style.INK,
              "unused": "white"}
    labels = {"deleted": "entries deleted normally", "random": "random bytes",
              "in use": "entries left in use", "unused": "unused"}
    missing = {}  # files the failed cycle could not find, from the logs
    from flashrel.config import load_campaign
    run_dir = Path(load_campaign(args.config).run_dir)
    for d in drives:
        path = run_dir / d / "cycles.csv"
        if path.exists():
            for row in csv.DictReader(open(path, encoding="utf-8")):
                if row["outcome"] != "pass" and int(row["files_missing"] or 0):
                    missing[d] = int(row["files_missing"])
    # a smaller copy with the same type, for a slide (--width/--height)
    size = (args.width or style.PAGE_W, args.height or 2.3)
    fig = plt.figure(figsize=size)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, getattr(args, "ratio_b", None) or 1.15],
                          wspace=0.3)
    ax = fig.add_subplot(gs[0])
    dir_drives = [d for d in ("A16-02", "A16-01") if drives.get(d, {}).get("dirs")]
    for y, d in enumerate(dir_drives):
        rel, info = next(iter(drives[d]["dirs"].items()))
        for r in info["runs"]:
            a, w = r["start"] / 1024, (r["end"] - r["start"]) / 1024
            if r["kind"] == "in use" and w < 0.25:  # a single entry: widen it to be seen
                a, w = a + w / 2 - 0.125, 0.25
            ax.barh(y, w, left=a, height=0.56, color=colors[r["kind"]],
                    edgecolor=style.MIST if r["kind"] == "unused" else "none",
                    linewidth=0.5, zorder=3 if r["kind"] == "in use" else 2)
        rand = [r for r in info["runs"] if r["kind"] == "random"]
        if rand:
            a, b = rand[0]["start"] / 1024, max(r["end"] for r in rand) / 1024
            ax.text((a + b) / 2, y - 0.33, f"{b - a:.0f} KiB of random bytes", ha="center",
                    va="bottom", fontsize=6, color=style.FAIL)
        if d in missing:
            ax.text(28, y, f"{missing[d]} files\nlost", ha="center", va="center", fontsize=6,
                    color=style.INK, linespacing=1.1)
    ax.set_yticks(range(len(dir_drives)))
    ax.set_yticklabels([f"{d}\n{next(iter(drives[d]['dirs']))[-5:]}" for d in dir_drives],
                       fontsize=6.5)
    ax.set_ylim(len(dir_drives) - 0.45, -1.25)
    ax.set_xlim(0, 32)
    ax.set_xticks([0, 8, 16, 24, 32])
    ax.set_xlabel("Offset in the folder's 32 KiB directory cluster (KiB)")
    for side in ("left", "top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.legend(handles=[Patch(facecolor=colors[k], edgecolor=style.MIST if k == "unused"
                             else colors[k], label=labels[k]) for k in colors],
              loc="upper center", bbox_to_anchor=(0.5, 1.02), ncol=2, fontsize=6,
              frameon=False, handlelength=1.2, columnspacing=1.2, borderaxespad=0)
    style.panel_label(ax, "a", x=-0.2, y=1.0)
    # (b) bits flipped in the first clusters
    bx = fig.add_subplot(gs[1])
    shown = [d for d in ("A8-03", "A8-04", "A8-02") if drives.get(d, {}).get("first_clusters")]
    shades = {"A8-03": "#E3B26F", "A8-04": "#B9741F", "A8-02": style.GROUP_COLORS["A8"]}
    markers = {"A8-03": "s", "A8-04": "D", "A8-02": "o"}
    ends, peak = [], 1.0
    for d in shown:
        rows = drives[d]["first_clusters"]
        xs = [i for i, r in enumerate(rows) if r["known_bits"]]
        ys = [100 * rows[i]["wrong_bits"] / rows[i]["known_bits"] for i in xs]
        bx.plot(xs, ys, marker=markers[d], ms=3.6, lw=1.0, color=shades[d], mec="white",
                mew=0.4, zorder=3)
        ends.append([ys[-1], f"{d}  {100 * drives[d]['first_clusters_flip']:.0f} %"])
        peak = max(peak, *ys)
    top = peak * 1.1
    gap = 0.07 * top  # labels at least one text line apart
    for k, item in enumerate(sorted(ends)):
        if k and item[0] - prev < gap:
            item[0] = prev + gap
        prev = item[0]
        bx.text(len(ROLES) - 0.75, item[0], item[1], va="center", ha="left", fontsize=6,
                color=style.INK)
    intact = [d for d, m in drives.items() if d not in shown and m.get("first_clusters")]
    if intact:
        bx.plot(range(len(ROLES)), [0.2] * len(ROLES), color=style.MIST, lw=1.0, zorder=2,
                clip_on=False)
        bx.text(len(ROLES) - 1, 0.03 * top, f"the {len(intact)} other drives: 0 % in every "
                "cluster", va="bottom", ha="right", fontsize=6, color=style.GRAY)
    bx.set_xticks(range(len(ROLES)))
    bx.set_xticklabels(SHORT_ROLES, fontsize=5.6)
    bx.set_xlim(-0.4, len(ROLES) - 0.6)
    bx.set_ylim(0, top)
    bx.set_ylabel("Bits flipped (%)")
    bx.grid(axis="y", color=style.GRID, lw=0.5)
    for side in ("top", "right"):
        bx.spines[side].set_visible(False)
    style.panel_label(bx, "b", x=-0.16, y=1.0)
    style.save(fig, out / args.name, width=size[0])
    if args.no_tex:
        print(f"wrote {out / args.name}.png")
        return 0
    # numbers for the report
    lines = ["% generated by Project_code/scripts/forensics.py; do not edit"]
    for d, m in sorted(drives.items()):
        if m.get("first_clusters_flip") is not None:
            lines.append(f"\\resdef{{forensics-{d}-flip}}{{{100 * m['first_clusters_flip']:.0f}}}")
        lines.append(f"\\resdef{{forensics-{d}-id-bits}}{{{m['identity']['wrong_bits']}}}")
        if "stale_data" in m:
            s = m["stale_data"]
            lines.append(f"\\resdef{{forensics-{d}-stale-mib}}{{{s['bytes_checked'] / 2**20:.0f}}}")
            lines.append(f"\\resdef{{forensics-{d}-stale-bits}}{{{s['wrong_bits']}}}")
    (out / "forensics.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out / 'fig_forensics.pdf'} and {out / 'forensics.tex'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("config")
    c.add_argument("--out", required=True)
    c.add_argument("--dir", action="append", help="DRIVE:folder whose directory cluster to save")
    f = sub.add_parser("figure")
    f.add_argument("config")
    f.add_argument("--data", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--width", type=float, help="figure width in inches (default: page width)")
    f.add_argument("--height", type=float, help="figure height in inches (default: 2.3)")
    f.add_argument("--name", default="fig_forensics", help="file name without extension")
    f.add_argument("--no-tex", action="store_true", help="skip forensics.tex (slide copies)")
    args = ap.parse_args(argv)
    return capture(args) if args.cmd == "capture" else figure(args)


if __name__ == "__main__":
    raise SystemExit(main())
