"""Map operating-system errors to a small set of failure-relevant kinds.

The same physical event surfaces as different error codes on each platform:
a drive that vanishes mid-write is ``ERROR_DEVICE_NOT_CONNECTED`` (1167) on
Windows and ``ENXIO``/``ENOENT`` on macOS. The harness only needs to know which
kind of event happened, so the codes are folded into :class:`ErrorKind`.
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
_WINERROR = {
    2: ErrorKind.NOT_FOUND,         # ERROR_FILE_NOT_FOUND
    3: ErrorKind.NOT_FOUND,         # ERROR_PATH_NOT_FOUND
    5: ErrorKind.OTHER,             # ERROR_ACCESS_DENIED: usually a transient lock (AV, indexer)
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
    112: ErrorKind.NO_SPACE,        # ERROR_DISK_FULL
    483: ErrorKind.IO_ERROR,        # ERROR_DEVICE_HARDWARE_ERROR
    1006: ErrorKind.DISCONNECTED,   # ERROR_FILE_INVALID (volume changed underneath)
    1117: ErrorKind.IO_ERROR,       # ERROR_IO_DEVICE
    1167: ErrorKind.DISCONNECTED,   # ERROR_DEVICE_NOT_CONNECTED
    1392: ErrorKind.FS_CORRUPT,     # ERROR_FILE_CORRUPT
    1393: ErrorKind.FS_CORRUPT,     # ERROR_DISK_CORRUPT
    1784: ErrorKind.IO_ERROR,       # ERROR_INVALID_USER_BUFFER
}

# EACCES/EPERM are deliberately absent: a permission error does not prove that
# the volume is write-protected, so read-only status is confirmed by probing
# the volume flags instead (see ``volumes.is_read_only``).
_ERRNO = {
    errno.EIO: ErrorKind.IO_ERROR,
    errno.EROFS: ErrorKind.READ_ONLY,
    errno.ENOSPC: ErrorKind.NO_SPACE,
    errno.ENXIO: ErrorKind.DISCONNECTED,
    errno.ENODEV: ErrorKind.DISCONNECTED,
    errno.ENOENT: ErrorKind.NOT_FOUND,
}
for _name, _kind in (("EUCLEAN", ErrorKind.FS_CORRUPT), ("ESHUTDOWN", ErrorKind.DISCONNECTED)):
    if hasattr(errno, _name):
        _ERRNO[getattr(errno, _name)] = _kind


def classify_os_error(exc: BaseException) -> ErrorKind:
    """Fold an exception raised by file I/O into an :class:`ErrorKind`."""
    if not isinstance(exc, OSError):
        return ErrorKind.OTHER
    winerror = getattr(exc, "winerror", None)
    if winerror is not None and winerror in _WINERROR:
        return _WINERROR[winerror]
    if exc.errno in _ERRNO:
        return _ERRNO[exc.errno]
    return ErrorKind.OTHER


def describe(exc: BaseException) -> str:
    """Short, single-line description for logs."""
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    text = getattr(exc, "strerror", None) or str(exc)
    return f"[{code}] {text}" if code is not None else text
