"""Win32 primitives through ctypes (imported on Windows only).

Everything here uses documented kernel32 calls. The unbuffered reader is the one that
ran the whole of run 1: ``CreateFileW`` with ``FILE_FLAG_NO_BUFFERING`` into a
page-aligned buffer, so every read-back reaches the drive and never the host cache.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
from collections.abc import Iterator
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HANDLE = wintypes.HANDLE
INVALID_HANDLE_VALUE = HANDLE(-1).value
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_BEGIN = 0
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
FILE_READ_ONLY_VOLUME = 0x00080000
ERROR_NO_MORE_FILES = 18
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
BUS_TYPES = {0: "unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre",
             7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
             14: "Virtual", 15: "FileBackedVirtual", 16: "Spaces", 17: "NVMe", 18: "SCM",
             19: "UFS"}


def _fn(lib, name, argtypes, restype):
    f = getattr(lib, name)
    f.argtypes = argtypes
    f.restype = restype
    return f


LPDWORD = ctypes.POINTER(wintypes.DWORD)
PULL = ctypes.POINTER(ctypes.c_ulonglong)
CreateFileW = _fn(k32, "CreateFileW", (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, HANDLE),
                  HANDLE)
ReadFile = _fn(k32, "ReadFile", (HANDLE, wintypes.LPVOID, wintypes.DWORD, LPDWORD, wintypes.LPVOID),
               wintypes.BOOL)
CloseHandle = _fn(k32, "CloseHandle", (HANDLE,), wintypes.BOOL)
SetFilePointerEx = _fn(k32, "SetFilePointerEx", (HANDLE, ctypes.c_longlong,
                                                 ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD),
                       wintypes.BOOL)
DeviceIoControl = _fn(k32, "DeviceIoControl", (HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                               wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                               LPDWORD, wintypes.LPVOID), wintypes.BOOL)
GetDriveTypeW = _fn(k32, "GetDriveTypeW", (wintypes.LPCWSTR,), wintypes.UINT)
GetVolumeInformationW = _fn(k32, "GetVolumeInformationW", (
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, LPDWORD, LPDWORD, LPDWORD,
    wintypes.LPWSTR, wintypes.DWORD), wintypes.BOOL)
GetDiskFreeSpaceW = _fn(k32, "GetDiskFreeSpaceW", (wintypes.LPCWSTR, LPDWORD, LPDWORD, LPDWORD,
                                                   LPDWORD), wintypes.BOOL)
GetDiskFreeSpaceExW = _fn(k32, "GetDiskFreeSpaceExW", (wintypes.LPCWSTR, PULL, PULL, PULL),
                          wintypes.BOOL)
FindFirstVolumeW = _fn(k32, "FindFirstVolumeW", (wintypes.LPWSTR, wintypes.DWORD), HANDLE)
FindNextVolumeW = _fn(k32, "FindNextVolumeW", (HANDLE, wintypes.LPWSTR, wintypes.DWORD),
                      wintypes.BOOL)
FindVolumeClose = _fn(k32, "FindVolumeClose", (HANDLE,), wintypes.BOOL)
GetVolumePathNamesForVolumeNameW = _fn(k32, "GetVolumePathNamesForVolumeNameW", (
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, LPDWORD), wintypes.BOOL)
GetVolumeNameForVolumeMountPointW = _fn(k32, "GetVolumeNameForVolumeMountPointW", (
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD), wintypes.BOOL)
SetErrorMode = _fn(k32, "SetErrorMode", (wintypes.UINT,), wintypes.UINT)
SetThreadExecutionState = _fn(k32, "SetThreadExecutionState", (wintypes.DWORD,), wintypes.DWORD)


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


GetSystemPowerStatus = _fn(k32, "GetSystemPowerStatus", (ctypes.POINTER(SYSTEM_POWER_STATUS),),
                           wintypes.BOOL)

# Probing an empty card-reader slot must not pop up "There is no disk in the drive".
SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)


def last_error(path: str = "") -> OSError:
    code = ctypes.get_last_error()
    return OSError(0, ctypes.FormatError(code).strip(), path, code)


def _open(path: str, access: int, flags: int = 0) -> int:
    handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                         flags, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise last_error(path)
    return handle


# -- data path ----------------------------------------------------------------------------------
def read_unbuffered(path: str, chunk_size: int) -> Iterator[bytes]:
    """Yield a file in ``chunk_size`` pieces read around the host cache.

    ``chunk_size`` must be a multiple of the sector size (4 KiB covers USB drives). The
    file system returns the short tail of a file without any special handling.
    """
    handle = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING,
                         FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise last_error(path)
    buffer = mmap.mmap(-1, chunk_size)  # anonymous mappings are page-aligned
    anchor = ctypes.c_char.from_buffer(buffer)
    nread = wintypes.DWORD()
    try:
        while True:
            if not ReadFile(handle, ctypes.addressof(anchor), chunk_size, ctypes.byref(nread), None):
                raise last_error(path)
            n = nread.value
            if n == 0:
                return
            yield buffer[:n]  # a copy, so no view outlives the buffer
            if n < chunk_size:
                return
    finally:
        del anchor
        buffer.close()
        CloseHandle(handle)


class RawWin:
    """Sector reads of a mounted volume (read-only, shared; nothing is ever written)."""

    ALIGN = 4096  # a multiple of every sector size in use (512 and 4096)

    def __init__(self, device_path: str) -> None:
        self.path = device_path
        self.handle = _open(device_path, GENERIC_READ, FILE_FLAG_NO_BUFFERING)

    def read(self, offset: int, length: int) -> bytes:
        start = offset // self.ALIGN * self.ALIGN
        end = -(-(offset + length) // self.ALIGN) * self.ALIGN
        size = end - start
        if not SetFilePointerEx(self.handle, start, None, FILE_BEGIN):
            raise last_error(self.path)
        buffer = mmap.mmap(-1, size)
        anchor = ctypes.c_char.from_buffer(buffer)
        nread = wintypes.DWORD()
        try:
            if not ReadFile(self.handle, ctypes.addressof(anchor), size, ctypes.byref(nread), None):
                raise last_error(self.path)
            data = buffer[: nread.value]
        finally:
            del anchor
            buffer.close()
        return data[offset - start: offset - start + length]

    def close(self) -> None:
        if self.handle:
            CloseHandle(self.handle)
            self.handle = None


# -- volumes ------------------------------------------------------------------------------------
def volume_information(root: str) -> dict | None:
    label = ctypes.create_unicode_buffer(261)
    fsname = ctypes.create_unicode_buffer(261)
    serial, maxlen, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if not GetVolumeInformationW(root, label, 261, ctypes.byref(serial), ctypes.byref(maxlen),
                                 ctypes.byref(flags), fsname, 261):
        return None
    return {"label": label.value, "fs": fsname.value, "vsn": serial.value,
            "read_only": bool(flags.value & FILE_READ_ONLY_VOLUME), "flags": flags.value}


def disk_free(root: str) -> tuple[int, int, int]:
    """(total bytes, free bytes available to the caller, cluster size)."""
    avail, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not GetDiskFreeSpaceExW(root, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)):
        raise last_error(root)
    spc, bps, nfree, ntotal = (wintypes.DWORD() for _ in range(4))
    cluster = 0
    if GetDiskFreeSpaceW(root, ctypes.byref(spc), ctypes.byref(bps), ctypes.byref(nfree),
                         ctypes.byref(ntotal)):
        cluster = spc.value * bps.value
    return total.value, avail.value, cluster or 4096


def drive_type(root: str) -> int:
    return int(GetDriveTypeW(root))


def volume_guids() -> list[str]:
    """Every volume of the host as ``\\\\?\\Volume{...}\\`` (with or without a drive letter)."""
    buf = ctypes.create_unicode_buffer(1024)
    handle = FindFirstVolumeW(buf, 1024)
    if handle in (None, INVALID_HANDLE_VALUE):
        return []
    found = []
    try:
        while True:
            found.append(buf.value)
            if not FindNextVolumeW(handle, buf, 1024):
                break
    finally:
        FindVolumeClose(handle)
    return found


def mount_paths(guid: str) -> list[str]:
    size = wintypes.DWORD(0)
    buf = ctypes.create_unicode_buffer(1024)
    if not GetVolumePathNamesForVolumeNameW(guid, buf, 1024, ctypes.byref(size)):
        return []
    raw = ctypes.wstring_at(ctypes.addressof(buf), max(0, size.value))
    return [p for p in raw.split("\x00") if p]


def guid_for_root(root: str) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    if GetVolumeNameForVolumeMountPointW(root, buf, 1024):
        return buf.value
    return ""


def _ioctl(device_path: str, code: int, inbuf: bytes, outsize: int) -> bytes:
    handle = _open(device_path, 0)
    try:
        src = ctypes.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
        out = ctypes.create_string_buffer(outsize)
        returned = wintypes.DWORD()
        if not DeviceIoControl(handle, code, src, len(inbuf), out, outsize, ctypes.byref(returned),
                               None):
            raise last_error(device_path)
        return out.raw[: returned.value]
    finally:
        CloseHandle(handle)


def _cstr(blob: bytes, offset: int) -> str:
    if not offset or offset >= len(blob):
        return ""
    end = blob.find(b"\x00", offset)
    return blob[offset: end if end >= 0 else len(blob)].decode("ascii", "replace").strip()


def storage_descriptor(device_path: str) -> dict:
    """IOCTL_STORAGE_QUERY_PROPERTY(StorageDeviceProperty): bus, vendor, product, serial."""
    query = struct.pack("<II4x", 0, 0)  # StorageDeviceProperty, PropertyStandardQuery
    blob = _ioctl(device_path, IOCTL_STORAGE_QUERY_PROPERTY, query, 1024)
    if len(blob) < 36:
        raise OSError(0, "short storage descriptor", device_path)
    removable = blob[10]
    vendor_off, product_off, rev_off, serial_off, bus = struct.unpack_from("<IIIII", blob, 12)
    return {"bus": BUS_TYPES.get(bus, f"type{bus}"), "removable_media": bool(removable),
            "vendor": _cstr(blob, vendor_off), "product": _cstr(blob, product_off),
            "revision": _cstr(blob, rev_off), "serial": _cstr(blob, serial_off)}


def device_number(device_path: str) -> tuple[int, int, int]:
    blob = _ioctl(device_path, IOCTL_STORAGE_GET_DEVICE_NUMBER, b"", 12)
    return struct.unpack_from("<III", blob, 0)


def device_path_for_root(root: str) -> str:
    """``E:\\`` -> ``\\\\.\\E:``; ``\\\\?\\Volume{..}\\`` -> ``\\\\?\\Volume{..}``."""
    if len(root) >= 2 and root[1] == ":":
        return "\\\\.\\" + root[:2]
    return root.rstrip("\\")


# -- host ---------------------------------------------------------------------------------------
def keep_awake(on: bool) -> None:
    SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def power_status() -> dict:
    status = SYSTEM_POWER_STATUS()
    if not GetSystemPowerStatus(ctypes.byref(status)):
        return {}
    return {"ac_line": {0: "battery", 1: "ac"}.get(status.ACLineStatus & 0xFF, "unknown"),
            "battery_percent": status.BatteryLifePercent & 0xFF}


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def system_disk_number() -> int | None:
    drive = os.environ.get("SystemDrive", "C:")
    try:
        return device_number("\\\\.\\" + drive[:2])[1]
    except OSError:
        return None
