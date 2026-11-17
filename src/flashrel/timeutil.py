"""Timestamps. Records carry UTC ISO-8601 times; durations come from monotonic clocks."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """UTC timestamp with milliseconds, e.g. ``2026-11-18T14:03:07.125+00:00``."""
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def local_iso(dt: datetime | None = None) -> str:
    """Local wall-clock time with its UTC offset (what the operator sees)."""
    return (dt or utc_now()).astimezone().isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    """A recorded timestamp. Anything that is not one (None, a number) is a ValueError,
    like a malformed string, so one ``except ValueError`` covers every bad record."""
    if not isinstance(text, str):
        raise ValueError(f"not a timestamp: {text!r}")
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seconds_between(a: str | datetime, b: str | datetime) -> float:
    a = parse_iso(a) if isinstance(a, str) else a
    b = parse_iso(b) if isinstance(b, str) else b
    return (b - a).total_seconds()
