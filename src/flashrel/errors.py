"""Fold operating-system errors into the few kinds that matter for failure analysis.

The same physical event surfaces as different error codes on each platform: a drive
that vanishes mid-write is ``ERROR_DEVICE_NOT_CONNECTED`` (1167) on Windows and
``ENXIO`` on macOS. The rig only needs to know which kind of event happened.
"""

from __future__ import annotations

import errno
from enum import Enum


class ErrorKind(str, Enum):
    IO_ERROR = "io_error"          # the device reported a read or write failure
    READ_ONLY = "read_only"        # writes rejected: write-protect / end-of-life lock
    DISCONNECTED = "disconnected"  # device or volume no longer present
    NO_SPACE = "no_space"          # volume full earlier than planned
    FS_CORRUPT = "fs_corrupt"      # file-system structures damaged
    NOT_FOUND = "not_found"        # a path vanished while the volume is still mounted
    OTHER = "other"


# Win32 error codes (winerror.h).
WINERROR = {
    2: ErrorKind.NOT_FOUND,         # ERROR_FILE_NOT_FOUND
    3: ErrorKind.NOT_FOUND,         # ERROR_PATH_NOT_FOUND
    5: ErrorKind.OTHER,             # ERROR_ACCESS_DENIED: usually a transient lock (AV, indexer)
    6: ErrorKind.DISCONNECTED,      # ERROR_INVALID_HANDLE: volume dismounted under an open file
    15: ErrorKind.DISCONNECTED,     # ERROR_INVALID_DRIVE
    19: ErrorKind.READ_ONLY,        # ERROR_WRITE_PROTECT
    21: ErrorKind.DISCONNECTED,     # ERROR_NOT_READY
    23: ErrorKind.IO_ERROR,         # ERROR_CRC (data error, cyclic redundancy check)
    27: ErrorKind.IO_ERROR,         # ERROR_SECTOR_NOT_FOUND
    29: ErrorKind.IO_ERROR,         # ERROR_WRITE_FAULT
    30: ErrorKind.IO_ERROR,         # ERROR_READ_FAULT
    31: ErrorKind.IO_ERROR,         # ERROR_GEN_FAILURE
    32: ErrorKind.OTHER,            # ERROR_SHARING_VIOLATION
    33: ErrorKind.OTHER,            # ERROR_LOCK_VIOLATION
    39: ErrorKind.NO_SPACE,         # ERROR_HANDLE_DISK_FULL
    55: ErrorKind.DISCONNECTED,     # ERROR_DEV_NOT_EXIST
    112: ErrorKind.NO_SPACE,        # ERROR_DISK_FULL
    145: ErrorKind.FS_CORRUPT,      # ERROR_DIR_NOT_EMPTY: run 1's orphaned directory entries
    483: ErrorKind.IO_ERROR,        # ERROR_DEVICE_HARDWARE_ERROR
    1005: ErrorKind.FS_CORRUPT,     # ERROR_UNRECOGNIZED_VOLUME
    1006: ErrorKind.DISCONNECTED,   # ERROR_FILE_INVALID (volume changed underneath)
    1117: ErrorKind.IO_ERROR,       # ERROR_IO_DEVICE
    1167: ErrorKind.DISCONNECTED,   # ERROR_DEVICE_NOT_CONNECTED
    1392: ErrorKind.FS_CORRUPT,     # ERROR_FILE_CORRUPT
    1393: ErrorKind.FS_CORRUPT,     # ERROR_DISK_CORRUPT
    1784: ErrorKind.IO_ERROR,       # ERROR_INVALID_USER_BUFFER
    1785: ErrorKind.FS_CORRUPT,     # ERROR_UNRECOGNIZED_MEDIA
}

# EACCES/EPERM are deliberately absent: a permission error does not prove that the
# volume is write-protected, so read-only status is confirmed from the volume flags.
ERRNO = {
    errno.EIO: ErrorKind.IO_ERROR,
    errno.EROFS: ErrorKind.READ_ONLY,
    errno.ENOSPC: ErrorKind.NO_SPACE,
    errno.ENXIO: ErrorKind.DISCONNECTED,
    errno.ENODEV: ErrorKind.DISCONNECTED,
    errno.ENOENT: ErrorKind.NOT_FOUND,
    errno.ENOTEMPTY: ErrorKind.FS_CORRUPT,
}
for _name, _kind in (("EUCLEAN", ErrorKind.FS_CORRUPT), ("ESHUTDOWN", ErrorKind.DISCONNECTED)):
    if hasattr(errno, _name):
        ERRNO[getattr(errno, _name)] = _kind


def classify(exc: BaseException) -> ErrorKind:
    if not isinstance(exc, OSError):
        return ErrorKind.OTHER
    winerror = getattr(exc, "winerror", None)
    if winerror is not None and winerror in WINERROR:
        return WINERROR[winerror]
    if exc.errno in ERRNO:
        return ERRNO[exc.errno]
    return ErrorKind.OTHER


def describe(exc: BaseException) -> str:
    """Short single-line description for the logs, e.g. ``[1392] The file ... is corrupted``."""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    text = (getattr(exc, "strerror", None) or str(exc)).strip()
    return f"[{code}] {text}" if code is not None else text
