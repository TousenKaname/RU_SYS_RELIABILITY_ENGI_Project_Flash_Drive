"""Find drives under test and query their volumes.

Drive letters (Windows) and mount points (macOS, Linux) change whenever a
drive is re-plugged, so the harness never relies on them. Each drive instead
carries an identity file written at enrollment; a drive is located by scanning
the mounted volumes for the file with its ID.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flashrel.system.directio import full_fsync

IDENTITY_FILE = "FLASHREL-ID.json"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _DRIVE_REMOVABLE, _DRIVE_FIXED = 2, 3
    _FILE_READ_ONLY_VOLUME = 0x00080000
    _SEM_FAILCRITICALERRORS, _SEM_NOOPENFILEERRORBOX = 0x0001, 0x8000
    # Probing an empty card-reader slot must not pop up "There is no disk in the drive".
    _k32.SetErrorMode(_SEM_FAILCRITICALERRORS | _SEM_NOOPENFILEERRORBOX)


@dataclass(frozen=True)
class VolumeUsage:
    total: int
    free: int
    cluster_size: int


def cluster_size(mount: Path) -> int:
    """Allocation unit of the file system (32 KiB for an 8 GB exFAT volume)."""
    if sys.platform == "win32":
        spc, bps, nfree, ntotal = (wintypes.DWORD() for _ in range(4))
        root = os.path.splitdrive(str(mount))[0] + "\\"
        if _k32.GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(nfree),
                                  ctypes.byref(ntotal)):
            return spc.value * bps.value
        return 4096
    st = os.statvfs(mount)
    return st.f_frsize or st.f_bsize or 4096


def usage(mount: Path) -> VolumeUsage:
    du = shutil.disk_usage(mount)
    return VolumeUsage(total=du.total, free=du.free, cluster_size=cluster_size(mount))


def is_read_only(mount: Path) -> bool:
    """True when the OS reports the volume as read-only (write-protect lock)."""
    if sys.platform == "win32":
        flags = wintypes.DWORD()
        root = os.path.splitdrive(str(mount))[0] + "\\"
        if not _k32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
            code = ctypes.get_last_error()
            raise OSError(0, ctypes.FormatError(code).strip(), root, code)
        return bool(flags.value & _FILE_READ_ONLY_VOLUME)
    return bool(os.statvfs(mount).f_flag & os.ST_RDONLY)


def filesystem_name(mount: Path) -> str:
    """File-system type of the volume, e.g. ``exFAT`` (empty string if unknown)."""
    if sys.platform == "win32":
        name = ctypes.create_unicode_buffer(64)
        root = os.path.splitdrive(str(mount))[0] + "\\"
        if _k32.GetVolumeInformationW(root, None, 0, None, None, None, name, 64):
            return name.value
        return ""
    target = str(Path(mount).resolve())
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/mounts").read_text().splitlines():
                parts = line.split()
                if len(parts) > 2 and parts[1].replace("\\040", " ") == target:
                    return parts[2]
        except OSError:
            return ""
        return ""
    try:  # macOS: "/dev/disk4s1 on /Volumes/A8-03 (exfat, local, nodev, nosuid, noowners)"
        output = subprocess.run(["mount"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in output.splitlines():
        if f" on {target} (" in line:
            return line.rsplit("(", 1)[1].split(",")[0].strip()
    return ""


def candidate_mounts() -> list[Path]:
    """Mounted volumes that could hold a drive under test."""
    if sys.platform == "win32":
        system_drive = os.environ.get("SystemDrive", "C:").upper()
        mask = _k32.GetLogicalDrives()
        roots = []
        for i, letter in enumerate(string.ascii_uppercase):
            root = f"{letter}:\\"
            if not mask >> i & 1 or letter in "AB" or root.startswith(system_drive):
                continue
            if _k32.GetDriveTypeW(root) in (_DRIVE_REMOVABLE, _DRIVE_FIXED):
                roots.append(Path(root))
        return roots
    if sys.platform == "darwin":
        base = Path("/Volumes")
        return [p for p in base.iterdir() if p.is_dir() and p.resolve() != Path("/")] \
            if base.exists() else []
    user = getpass.getuser()
    found: list[Path] = []
    for base in (Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt")):
        if base.is_dir():
            found.extend(p for p in base.iterdir() if p.is_dir() and os.path.ismount(p))
    return found


def read_identity(mount: Path) -> dict | None:
    try:
        return json.loads((mount / IDENTITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_identity(mount: Path, drive_id: str, **extra: object) -> dict:
    """Mark a volume as test drive ``drive_id`` (done once, at enrollment)."""
    record = {
        "drive_id": drive_id,
        "enrolled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        **extra,
    }
    data = json.dumps(record, indent=2).encode("utf-8")
    fd = os.open(mount / IDENTITY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                 | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, data)
        full_fsync(fd)
    finally:
        os.close(fd)
    return record


def find_drive(drive_id: str) -> Path | None:
    """Mount point of the volume whose identity file names ``drive_id``."""
    for mount in candidate_mounts():
        ident = read_identity(mount)
        if ident and ident.get("drive_id") == drive_id:
            return mount
    return None


def wait_for_drive(drive_id: str, timeout_s: float, *, poll_s: float = 2.0,
                   stop: threading.Event | None = None) -> Path | None:
    """Poll until the drive is mounted again (after a disconnect or re-plug)."""
    deadline = time.monotonic() + timeout_s
    while True:
        mount = find_drive(drive_id)
        if mount is not None or time.monotonic() >= deadline:
            return mount
        if stop is not None and stop.wait(poll_s):
            return None
        if stop is None:
            time.sleep(poll_s)
