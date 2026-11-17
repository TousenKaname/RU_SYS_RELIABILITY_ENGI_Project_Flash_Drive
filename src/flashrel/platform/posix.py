"""macOS / Linux backend for development. The real rig runs on Windows.

File I/O is real; reads can bypass the page cache (``F_NOCACHE`` on macOS,
``posix_fadvise`` on Linux), but there is no device management.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

from flashrel.platform.base import Platform, Volume, _read_full


class PosixPlatform(Platform):
    name = "posix"

    def list_volumes(self) -> list[Volume]:
        bases = ["/Volumes"] if sys.platform == "darwin" else ["/media", "/run/media", "/mnt"]
        found: list[Volume] = []
        for base in bases:
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                path = os.path.join(base, name)
                if not os.path.isdir(path) or os.path.realpath(path) == "/":
                    continue
                try:
                    total, free, cluster = self.usage(path)
                except OSError:
                    continue
                found.append(Volume(root=path + os.sep, label=name, total=total, free=free,
                                    cluster=cluster, removable=True))
        return found

    def read_chunks(self, path: str, chunk_size: int, *, uncached: bool = True) -> Iterator[bytes]:
        fd = os.open(path, os.O_RDONLY)
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

    def cache_bypass(self) -> bool:
        return sys.platform == "darwin" or hasattr(os, "posix_fadvise")
