"""Cache-bypassing reads and durable writes.

A file read right after it was written normally comes back from the copy the
operating system still holds in RAM, so the "verification" never touches the
drive. The reader below bypasses the page cache on each platform:

* Windows: ``CreateFileW`` with ``FILE_FLAG_NO_BUFFERING`` into a page-aligned
  buffer (sector-aligned, as the flag requires);
* macOS: the flushed file's pages are invalidated with ``msync(MS_INVALIDATE)``
  (:func:`evict_cache`) and the reader sets ``fcntl(F_NOCACHE)``;
* Linux: ``posix_fadvise(POSIX_FADV_DONTNEED)``, which evicts the clean pages
  of a file that has just been synced.

Writes are flushed with ``fsync`` (``F_FULLFSYNC`` on macOS, which also empties
the device's own write cache), so the drive has the data before it is read back.
On macOS and Linux, :func:`device_reads` lets the caller *measure* that the
read-back caused device reads instead of trusting the mechanism.
"""

from __future__ import annotations

import errno
import mmap
import os
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

_O_BINARY = getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _k32.CreateFileW
    _CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _CreateFileW.restype = wintypes.HANDLE
    _ReadFile = _k32.ReadFile
    _ReadFile.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    _ReadFile.restype = wintypes.BOOL
    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _OPEN_EXISTING = 3
    _FILE_FLAG_NO_BUFFERING = 0x20000000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000

    def _last_error(path: Path) -> OSError:
        code = ctypes.get_last_error()
        return OSError(0, ctypes.FormatError(code).strip(), str(path), code)


def cache_bypass_supported() -> bool:
    """True when :func:`read_chunks` can bypass the page cache on this host."""
    return sys.platform in ("win32", "darwin") or hasattr(os, "posix_fadvise")


def read_chunks(path: Path, chunk_size: int, *, uncached: bool = True) -> Iterator[bytes]:
    """Yield a file in ``chunk_size`` pieces; the last piece may be shorter.

    ``chunk_size`` must be a multiple of the sector size (4 KiB covers every
    USB drive) when ``uncached`` is true on Windows.
    """
    if uncached and sys.platform == "win32":
        yield from _read_windows_unbuffered(path, chunk_size)
    else:
        yield from _read_posix(path, chunk_size, uncached=uncached)


def _read_windows_unbuffered(path: Path, chunk_size: int) -> Iterator[bytes]:
    handle = _CreateFileW(str(path), _GENERIC_READ, _FILE_SHARE_READ, None, _OPEN_EXISTING,
                          _FILE_FLAG_NO_BUFFERING | _FILE_FLAG_SEQUENTIAL_SCAN, None)
    if handle in (None, _INVALID_HANDLE):
        raise _last_error(path)
    buffer = mmap.mmap(-1, chunk_size)  # anonymous mappings are page-aligned
    anchor = ctypes.c_char.from_buffer(buffer)
    nread = wintypes.DWORD()
    try:
        while True:
            if not _ReadFile(handle, ctypes.addressof(anchor), chunk_size, ctypes.byref(nread),
                             None):
                raise _last_error(path)
            n = nread.value
            if n == 0:
                return
            yield buffer[:n]  # a copy, so no view outlives the buffer
            if n < chunk_size:
                return
    finally:
        del anchor
        buffer.close()
        _CloseHandle(handle)


def _read_posix(path: Path, chunk_size: int, *, uncached: bool) -> Iterator[bytes]:
    fd = os.open(path, os.O_RDONLY | _O_BINARY)
    try:
        if uncached and sys.platform == "darwin":
            import fcntl

            fcntl.fcntl(fd, getattr(fcntl, "F_NOCACHE", 48), 1)
        elif uncached and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        while True:
            data = _read_full(fd, chunk_size)
            if not data:
                return
            yield data
            if len(data) < chunk_size:
                return
    finally:
        os.close(fd)


def _read_full(fd: int, n: int) -> bytes:
    parts: list[bytes] = []
    remaining = n
    while remaining:
        part = os.read(fd, remaining)
        if not part:
            break
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


#: While a file is written with ``sync``, flush every this many bytes, so that a
#: slow drive never holds hundreds of MB of unflushed data (watchdog-friendly).
SYNC_EVERY = 64 * 1024 * 1024
_FULLFSYNC_UNSUPPORTED = {errno.ENOTSUP, errno.EINVAL, errno.ENOTTY,
                          getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}


def full_fsync(fd: int) -> None:
    """Flush a file to the device (and the device cache, where the OS allows).

    On macOS ``F_FULLFSYNC`` also empties the drive's own cache. Only "not
    supported" errors fall back to ``fsync``; an I/O error is raised, never
    swallowed.
    """
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as exc:
            if exc.errno not in _FULLFSYNC_UNSUPPORTED:
                raise
    os.fsync(fd)


def write_file(path: Path, chunks: Iterable[bytes], *, sync: bool,
               sync_every: int = SYNC_EVERY) -> int:
    """Create ``path`` from ``chunks``; flush it to the device when ``sync``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o644)
    total = unsynced = 0
    try:
        for chunk in chunks:
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view):]
            total += len(chunk)
            unsynced += len(chunk)
            if sync and unsynced >= sync_every:
                full_fsync(fd)
                unsynced = 0
        if sync:
            full_fsync(fd)
    finally:
        os.close(fd)
    return total


def sync_files(paths: Iterable[Path]) -> None:
    """Flush files written without per-file sync (the ``per_phase`` policy).

    ``os.sync`` waits for completion only on Linux, so other systems flush
    file by file.
    """
    if sys.platform.startswith("linux"):
        os.sync()
        return
    for path in paths:
        fd = os.open(path, (os.O_RDONLY if sys.platform == "darwin" else os.O_RDWR) | _O_BINARY)
        try:
            full_fsync(fd)
        finally:
            os.close(fd)


def evict_cache(path: Path) -> bool:
    """Drop a flushed file's pages from the host cache, so the read-back hits the drive.

    macOS keeps just-written pages cached even for an ``F_NOCACHE`` reader, so
    they are invalidated with ``msync(MS_INVALIDATE)`` on a mapping of the file
    (the method ``vmtouch`` uses). Linux uses ``posix_fadvise(DONTNEED)``.
    Windows needs nothing: unbuffered reads always go to the device.
    Returns True when an eviction was performed.
    """
    if sys.platform == "darwin":
        _msync_invalidate(path)
        return True
    if hasattr(os, "posix_fadvise"):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
        return True
    return False


def _msync_invalidate(path: Path) -> None:
    import ctypes
    import ctypes.util

    size = os.path.getsize(path)
    if size == 0:
        return
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int64)
    libc.msync.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    libc.munmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
    prot_read, map_shared, ms_invalidate = 0x1, 0x1, 0x2  # <sys/mman.h> on Darwin
    fd = os.open(path, os.O_RDONLY)
    try:
        addr = libc.mmap(None, size, prot_read, map_shared, fd, 0)
        if addr is None or addr == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_errno(), "mmap failed", str(path))
        try:
            if libc.msync(ctypes.c_void_p(addr), size, ms_invalidate) != 0:
                raise OSError(ctypes.get_errno(), "msync(MS_INVALIDATE) failed", str(path))
        finally:
            libc.munmap(ctypes.c_void_p(addr), size)
    finally:
        os.close(fd)


def device_reads() -> int | None:
    """Block-input count of this process (``ru_inblock``); None where unavailable.

    A read-back that really reached the drive raises this counter, so the
    difference over the verify phase shows whether the cache was bypassed.
    """
    try:
        import resource
    except ImportError:  # Windows
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_inblock
