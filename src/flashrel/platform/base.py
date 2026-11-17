"""Operating-system interface used by the rig.

Three implementations exist: ``windows`` (the real rig), ``posix`` (macOS/Linux, for
development: real file I/O, no device management) and ``sim`` (simulated drives in
folders, with injected faults, used by the tests and the demo mode).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from typing import Any


class Unsupported(RuntimeError):
    """The operation is not available on this platform or without administrator rights."""


@dataclass
class Volume:
    root: str                 # path used for file operations, ends with a separator
    letter: str = ""          # "E" on Windows, "" otherwise
    guid: str = ""            # \\?\Volume{...}\ on Windows
    label: str = ""
    fs: str = ""
    total: int = 0
    free: int = 0
    cluster: int = 0
    removable: bool = False
    read_only: bool = False
    vsn: int | None = None    # volume serial number

    @property
    def display(self) -> str:
        return f"{self.letter}:" if self.letter else self.root

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StorageInfo:
    bus: str = "unknown"      # "USB", "SATA", "NVMe", ...
    removable_media: bool = False
    vendor: str = ""
    product: str = ""
    revision: str = ""
    serial: str = ""
    disk_number: int | None = None
    system_disk: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class RawVolume(AbstractContextManager):
    """Read-only access to the sectors of a mounted volume."""

    sector_size = 512

    def read(self, offset: int, length: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __exit__(self, *exc: object) -> None:
        self.close()


class FileRaw(RawVolume):
    """A disk image file as a raw volume (tests, forensics replay)."""

    def __init__(self, path: str) -> None:
        self._fh = open(path, "rb")

    def read(self, offset: int, length: int) -> bytes:
        self._fh.seek(offset)
        return self._fh.read(length)

    def close(self) -> None:
        self._fh.close()


class Platform:
    """Default implementation: plain file I/O, no device management."""

    name = "base"
    supports_format = False
    supports_reset = False
    supports_inventory = False

    # -- volumes ----------------------------------------------------------------------------
    def list_volumes(self) -> list[Volume]:
        return []

    def volume_at(self, root: str) -> Volume | None:
        """The volume at ``root`` (Windows looks at that one volume only)."""
        norm = os.path.normpath(root)
        return next((v for v in self.list_volumes() if os.path.normpath(v.root) == norm), None)

    def usage(self, root: str) -> tuple[int, int, int]:
        """(total, free, cluster) bytes of the volume holding ``root``."""
        st = os.statvfs(root)
        cluster = st.f_frsize or st.f_bsize or 4096
        return st.f_blocks * cluster, st.f_bavail * cluster, cluster

    def is_read_only(self, root: str) -> bool:
        return bool(os.statvfs(root).f_flag & getattr(os, "ST_RDONLY", 1))

    def present(self, root: str) -> bool:
        return os.path.isdir(root)

    def storage_info(self, root: str) -> StorageInfo | None:
        return None

    def raw_volume(self, vol: Volume) -> RawVolume:
        raise Unsupported("raw volume access is not available on this platform")

    # -- data path --------------------------------------------------------------------------
    def read_chunks(self, path: str, chunk_size: int, *, uncached: bool = True) -> Iterator[bytes]:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            while True:
                data = _read_full(fd, chunk_size)
                if not data:
                    return
                yield data
                if len(data) < chunk_size:
                    return
        finally:
            os.close(fd)

    def write_file(self, path: str, chunks: Iterable[bytes], *, sync: bool = True) -> int:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o644)
        total = 0
        try:
            for chunk in chunks:
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
                total += len(chunk)
            if sync:
                os.fsync(fd)
        finally:
            os.close(fd)
        return total

    def cache_bypass(self) -> bool:
        return False

    def make_dirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def remove_file(self, path: str) -> None:
        os.remove(path)

    def remove_dir(self, path: str) -> None:
        os.rmdir(path)

    # -- devices and host -------------------------------------------------------------------
    def inventory(self) -> list[dict[str, Any]]:
        return []

    def format_volume(self, vol: Volume, label: str) -> dict[str, Any]:
        raise Unsupported("formatting needs the Windows rig with administrator rights")

    def reset_device(self, instance_id: str) -> dict[str, Any]:
        raise Unsupported("device reset needs the Windows rig with administrator rights")

    def keep_awake(self, on: bool) -> None:
        pass

    def is_admin(self) -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def host_info(self) -> dict[str, Any]:
        import platform as _p

        return {"node": _p.node(), "os": f"{_p.system()} {_p.release()}", "machine": _p.machine(),
                "python": sys.version.split()[0], "executable": sys.executable}

    def python_executable(self) -> str:
        return sys.executable

    def spawn(self, argv: list[str], log_path: str, cwd: str | None = None,
              env: dict[str, str] | None = None) -> subprocess.Popen:
        log = open(log_path, "ab", buffering=0)
        try:
            return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
        finally:
            log.close()

    def open_url(self, url: str) -> None:
        import webbrowser

        webbrowser.open(url)


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
