"""Common-cause screening: was it the drive, or the hub or computer it hangs on?

A fault in a dock, a port or a host tends to hit several drives at once,
while a worn-out drive fails on its own. Cycle failures of two or more drives
that share a host and hub within a short window are therefore flagged for the
attribution protocol (port check with the reference drive) before any of them
is counted as a drive failure.
"""

from __future__ import annotations

import re

import pandas as pd


def hub_of(port: str) -> str:
    """Hub label from a port label: ``"A2"`` -> ``"A"``, ``"W-direct-1"`` -> ``"W-direct"``."""
    return re.sub(r"[-_]?\d+$", "", str(port)) or str(port)


def flag_common_cause(failures: pd.DataFrame, window: str = "10min") -> pd.DataFrame:
    """Mark cycle failures that cluster on one host and hub within ``window``.

    ``failures`` needs columns ``drive_id``, ``time`` (ISO strings or datetimes),
    ``host`` and ``port``. Returns a copy with ``hub``, ``cluster`` (an integer per
    host-hub burst) and ``common_cause`` (True when the burst spans >= 2 drives).
    """
    df = failures.copy()
    if df.empty:
        return df.assign(hub=[], cluster=[], common_cause=[])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df["hub"] = df["port"].map(hub_of)
    df = df.sort_values(["host", "hub", "time"]).reset_index(drop=True)
    gap = pd.Timedelta(window)
    new_burst = (df["host"] != df["host"].shift()) | (df["hub"] != df["hub"].shift()) | \
                (df["time"] - df["time"].shift() > gap)
    df["cluster"] = new_burst.cumsum()
    drives = df.groupby("cluster")["drive_id"].transform("nunique")
    df["common_cause"] = drives >= 2
    return df
