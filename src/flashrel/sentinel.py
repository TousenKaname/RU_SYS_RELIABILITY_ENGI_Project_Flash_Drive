"""Identity, data at rest and file-system records: located, re-read and checked.

At intake every drive receives two files that are never changed again:

* ``FLASHREL-ID.json``: who the drive is (drive ID, campaign, a random token);
* ``FLASHREL-SENTINEL.bin``: 1 MiB of reproducible data, written right after it, so
  both lie next to the file-system records at the start of the volume.

The worker re-reads both around the host cache every 10 minutes, at every phase
boundary and at the start of every cycle, and checks the exFAT boot region, up-case
table and root directory raw (see ``exfat``). In run 1 the A8 drives lost exactly these
records, and the loss was found by hand hours after it happened.

A finding counts only when it persists over ``confirm_reads`` reads ``confirm_gap_s``
apart, so a read that races a legitimate update of the root directory never counts.
A failing raw check is never trusted unless the same check passed at intake.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from flashrel import exfat
from flashrel.config import SentinelSpec
from flashrel.payload import sentinel_bytes
from flashrel.platform.base import Platform, Unsupported, Volume
from flashrel.timeutil import iso
from flashrel.verify import bit_flips

IDENTITY_FILE = "FLASHREL-ID.json"
SENTINEL_FILE = "FLASHREL-SENTINEL.bin"
READ_CHUNK = 1024 * 1024


@dataclass
class SentinelResult:
    time: str
    ok: bool
    failed: str = ""           # identity, sentinel, boot, upcase, rootdir, vanished
    code: str = ""             # F1 data at rest, F6 file-system records, F2 read error, F3 gone
    detail: str = ""
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reads: int = 1
    elapsed_s: float = 0.0
    dirty: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def identity_record(intake: dict[str, Any]) -> bytes:
    return base64.b64decode(intake["identity_b64"])


def read_identity(vol: Volume) -> dict[str, Any] | None:
    """The identity file as the OS returns it (cached read; used only to locate drives)."""
    try:
        with open(os.path.join(vol.root, IDENTITY_FILE), "rb") as fh:
            data = fh.read(65536)
        return json.loads(data.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def locate(platform: Platform, drive_id: str, intake: dict[str, Any] | None,
           volumes: list[Volume] | None = None) -> tuple[Volume | None, str]:
    """Find a drive: by its identity file first, by its USB serial number second.

    Returns (volume, how) with ``how`` in {"identity", "serial", ""}. A drive found only
    by serial is present but has lost or damaged its identity file.
    """
    volumes = platform.list_volumes() if volumes is None else volumes
    token = (intake or {}).get("token")
    for vol in volumes:
        if not vol.fs:
            continue
        ident = read_identity(vol)
        if ident and ident.get("drive_id") == drive_id and (token is None or ident.get("token") == token):
            return vol, "identity"
    serial = (intake or {}).get("usb_serial")
    if serial and (intake or {}).get("serial_unique", True):
        for vol in volumes:
            info = platform.storage_info(vol.root)
            if info is not None and info.serial and info.serial == serial:
                return vol, "serial"
    return None, ""


def _uncached(platform: Platform, path: str) -> bytes:
    return b"".join(platform.read_chunks(path, READ_CHUNK, uncached=True))


def _compare(expected: bytes, observed: bytes) -> dict[str, Any]:
    n = min(len(expected), len(observed))
    bad_bytes = sum(1 for x, y in zip(expected[:n], observed[:n]) if x != y) + abs(len(expected) - len(observed))
    one_to_zero = zero_to_one = 0
    if bad_bytes:
        a = int.from_bytes(expected[:n], "little")
        b = int.from_bytes(observed[:n], "little")
        one_to_zero = (a & ~b).bit_count()
        zero_to_one = (~a & b & ((1 << (8 * n)) - 1)).bit_count()
    return {"ok": bad_bytes == 0, "bytes": len(expected), "read": len(observed),
            "bad_bytes": bad_bytes, "wrong_bits": bit_flips(expected[:n], observed[:n]),
            "one_to_zero": one_to_zero, "zero_to_one": zero_to_one}


class SentinelChecker:
    def __init__(self, platform: Platform, intake: dict[str, Any], spec: SentinelSpec,
                 *, sleep=time.sleep, cancelled=lambda: False) -> None:
        self.platform = platform
        self.intake = intake
        self.spec = spec
        self.sleep = sleep
        self.cancelled = cancelled  # a stop during the confirm reads voids the finding
        self.expected_identity = identity_record(intake)
        self.expected_sentinel = sentinel_bytes(intake["drive_id"], intake["token"],
                                                int(intake["sentinel_size"]))
        base = intake.get("exfat") or {}
        self.baseline = base if spec.raw_checks and base.get("ok") else None

    # -- one pass ---------------------------------------------------------------------------
    def _pass(self, vol: Volume, only: set[str] | None = None,
              skip: set[str] = frozenset()) -> dict[str, dict[str, Any]]:
        comps: dict[str, dict[str, Any]] = {}
        files = ((IDENTITY_FILE, "identity", self.expected_identity),
                 (SENTINEL_FILE, "sentinel", self.expected_sentinel))
        for name, comp, expected in files:
            if (only is not None and comp not in only) or comp in skip:
                continue
            path = os.path.join(vol.root, name)
            try:
                comps[comp] = _compare(expected, _uncached(self.platform, path))
            except FileNotFoundError:
                comps[comp] = {"ok": False, "missing": True, "detail": f"{name} not found"}
            except OSError as exc:
                comps[comp] = {"ok": False, "error": True, "detail": f"{name}: {exc}"}
        if self.baseline is not None and (only is None or only & {"boot", "upcase", "rootdir"}):
            try:
                with self.platform.raw_volume(vol) as raw:
                    raw_result = exfat.check(raw, self.baseline["record"])
                for comp in ("boot", "upcase", "rootdir"):
                    if (only is None or comp in only) and comp not in skip:
                        comps[comp] = raw_result[comp]
                comps.setdefault("_info", {})["dirty"] = raw_result.get("dirty")
            except (OSError, Unsupported, exfat.ExfatError) as exc:
                comps["_raw_unavailable"] = {"ok": True, "detail": str(exc)[:200]}
        return comps

    def check(self, vol: Volume, skip: set[str] = frozenset()) -> SentinelResult:
        """``skip``: components whose damage is already on record (only new damage counts)."""
        t0 = time.monotonic()
        result = SentinelResult(time=iso(), ok=True)
        comps = self._pass(vol, skip=skip)
        for comp in skip:  # damage already on record: shown, not re-counted
            comps[comp] = {"ok": True, "skipped": True, "detail": "damage on record"}
        bad = {c for c, r in comps.items() if not c.startswith("_") and not r.get("ok")}
        reads = 1
        while bad and reads < max(1, self.spec.confirm_reads):
            if not self.platform.present(vol.root):
                break
            self.sleep(self.spec.confirm_gap_s)
            again = self._pass(vol, only=bad, skip=skip)
            reads += 1
            for comp in list(bad):
                if again.get(comp, {}).get("ok"):
                    result.warnings.append(f"{comp}: anomaly cleared on re-read ({comps[comp]})")
                    bad.discard(comp)
                else:
                    comps[comp] = again.get(comp, comps[comp])
        if bad and self.cancelled():
            result.warnings.append(f"anomaly in {', '.join(sorted(bad))} not confirmed: the check was "
                                   "interrupted by a stop request")
            bad = set()
        result.reads = reads
        result.components = {k: v for k, v in comps.items() if not k.startswith("_")}
        result.dirty = comps.get("_info", {}).get("dirty")
        if "_raw_unavailable" in comps:
            result.warnings.append(f"raw check unavailable: {comps['_raw_unavailable']['detail']}")
        if bad:
            if not self.platform.present(vol.root):
                result.ok, result.failed, result.code = False, "vanished", "F3"
                result.detail = "the volume disappeared during the check"
            else:
                result.ok = False
                result.failed, result.code = _classify(bad, comps)
                result.detail = "; ".join(f"{c}: {_short(comps[c])}" for c in sorted(bad))
        result.elapsed_s = round(time.monotonic() - t0, 3)
        return result


def _classify(bad: set[str], comps: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Most specific finding first: wrong data at rest (F1), then file-system records (F6),
    then a file that cannot be read at all (F2)."""
    for comp in ("identity", "sentinel"):
        if comp in bad and comps[comp].get("bad_bytes"):
            return comp, "F1"
    for comp in ("boot", "upcase", "rootdir"):
        if comp in bad:
            return comp, "F6"
    for comp in ("identity", "sentinel"):
        if comp in bad and comps[comp].get("missing"):
            return comp, "F6"
    comp = sorted(bad)[0]
    return comp, "F2"


def _short(r: dict[str, Any]) -> str:
    if r.get("bad_bytes"):
        return f"{r['bad_bytes']} of {r['bytes']} bytes wrong ({r['wrong_bits']} bits)"
    return str(r.get("detail") or "failed")[:160]


def intake_baseline(platform: Platform, vol: Volume) -> dict[str, Any]:
    """Raw exFAT baseline for the intake record; ``ok`` False switches raw checks off."""
    try:
        with platform.raw_volume(vol) as raw:
            record = exfat.baseline(raw, identity_name=IDENTITY_FILE, sentinel_name=SENTINEL_FILE)
            # The fresh baseline must pass its own check, or the check is not trusted.
            first = exfat.check(raw, record)
        if not all(first[c]["ok"] for c in ("boot", "upcase", "rootdir")):
            return {"ok": False, "error": f"self-check failed at intake: {first}"}
        return {"ok": True, "record": record}
    except (OSError, Unsupported, exfat.ExfatError) as exc:
        return {"ok": False, "error": str(exc)[:300]}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
