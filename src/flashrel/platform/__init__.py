"""Pick the operating-system backend: Windows (the rig), POSIX (development), or simulation."""

from __future__ import annotations

import sys

from flashrel.platform.base import Platform, RawVolume, StorageInfo, Unsupported, Volume


def get_platform(sim_root: str | None = None, speed: float = 1.0) -> Platform:
    if sim_root:
        from flashrel.platform.sim import SimPlatform

        return SimPlatform(sim_root, speed)
    if sys.platform == "win32":
        from flashrel.platform.windows import WindowsPlatform

        return WindowsPlatform()
    from flashrel.platform.posix import PosixPlatform

    return PosixPlatform()


__all__ = ["Platform", "RawVolume", "StorageInfo", "Unsupported", "Volume", "get_platform"]
