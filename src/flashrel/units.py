"""Byte sizes, durations and throughput: parsing and formatting.

Sizes in the configuration are strings such as ``"64KiB"`` or ``"2GiB"``. Binary
prefixes (KiB, MiB, GiB) are powers of 1024, decimal ones (kB, MB, GB) powers of 1000.
Throughput is reported in decimal MB/s (10**6 bytes/s), the convention of drive vendors.
"""

from __future__ import annotations

import re

KiB = 1024
MiB = 1024**2
GiB = 1024**3
MB = 1000**2
GB = 1000**3

_UNITS = {"": 1, "b": 1, "kib": KiB, "mib": MiB, "gib": GiB, "tib": 1024**4,
          "kb": 1000, "mb": MB, "gb": GB, "tb": 1000**4}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(value: int | float | str) -> int:
    """Byte count of an int or of a string such as ``"1.5GiB"``."""
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"size must be non-negative: {value!r}")
        return int(value)
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ValueError(f"cannot parse size: {value!r}")
    number, unit = match.groups()
    try:
        factor = _UNITS[unit.lower()]
    except KeyError:
        raise ValueError(f"unknown size unit {unit!r} in {value!r}") from None
    return int(round(float(number) * factor))


def format_bytes(n: float) -> str:
    """``7.4 GiB`` style (binary prefixes)."""
    for unit, factor in (("TiB", 1024**4), ("GiB", GiB), ("MiB", MiB), ("KiB", KiB)):
        if abs(n) >= factor:
            return f"{n / factor:.1f} {unit}"
    return f"{int(n)} B"


def format_duration(seconds: float) -> str:
    """``3d 04:05:06`` / ``04:05:06`` style."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    core = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {core}" if days else core


def mb_per_s(n_bytes: float, seconds: float) -> float:
    """Throughput in decimal MB/s; zero when no time elapsed."""
    return n_bytes / seconds / MB if seconds > 0 else 0.0
