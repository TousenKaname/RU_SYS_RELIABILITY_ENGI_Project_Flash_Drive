"""Byte-size constants, parsing and formatting.

Sizes in configuration files are written as human-readable strings such as
``"64KiB"`` or ``"2GiB"``. Binary prefixes (KiB, MiB, GiB) are powers of 1024;
decimal prefixes (kB, MB, GB) are powers of 1000. Throughput is always reported
in decimal megabytes per second (1 MB/s = 10**6 bytes/s), the convention used
by drive vendors.
"""

from __future__ import annotations

import re

KiB = 1024
MiB = 1024**2
GiB = 1024**3
MB = 1000**2
GB = 1000**3

_UNITS = {
    "": 1,
    "b": 1,
    "kib": KiB,
    "mib": MiB,
    "gib": GiB,
    "tib": 1024**4,
    "kb": 1000,
    "mb": MB,
    "gb": GB,
    "tb": 1000**4,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")


def parse_size(value: int | float | str) -> int:
    """Return a byte count from an int or a string such as ``"1.5GiB"``."""
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"size must be non-negative: {value!r}")
        return int(value)
    match = _SIZE_RE.match(value)
    if not match:
        raise ValueError(f"cannot parse size: {value!r}")
    number, unit = match.groups()
    try:
        factor = _UNITS[unit.lower()]
    except KeyError:
        raise ValueError(f"unknown size unit {unit!r} in {value!r}") from None
    return int(round(float(number) * factor))


def format_bytes(n: float) -> str:
    """Format a byte count with a binary prefix, e.g. ``7.4 GiB``."""
    for unit, factor in (("GiB", GiB), ("MiB", MiB), ("KiB", KiB)):
        if abs(n) >= factor:
            return f"{n / factor:.1f} {unit}"
    return f"{int(n)} B"


def mb_per_s(n_bytes: int, seconds: float) -> float:
    """Throughput in decimal MB/s; zero when no time elapsed."""
    return n_bytes / seconds / MB if seconds > 0 else 0.0
