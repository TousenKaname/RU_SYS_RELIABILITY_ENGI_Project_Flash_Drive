"""Turn campaign logs into analysis tables.

Two failure times are recorded for every drive:

* ``t_first`` - cycle of the first drive-attributed cycle failure (soft or hard);
* ``t_hard``  - cycle of the hard failure that ended the drive's life.

A cycle failure that was later traced to a port or host (an F5 ``port_fault``
event) is not counted against the drive. Drives without the event are
right-censored at their last completed cycle.
"""

from __future__ import annotations

import pandas as pd

from flashrel.config import Campaign
from flashrel.failure import EventType
from flashrel.recorder import DriveLog, drive_ids

NUMERIC_CYCLE_COLUMNS = (
    "cycle", "duration_s", "planned_files", "planned_bytes", "free_before", "free_after",
    "write_bytes", "write_s", "write_mbps", "write_lat_p50_s", "write_lat_p95_s",
    "write_lat_max_s", "verify_bytes", "verify_s", "verify_mbps", "verify_lat_p95_s", "delete_s",
    "files_missing", "files_corrupt", "files_unreadable", "bytes_corrupt", "bits_corrupt",
    "transient_errors", "unrecovered_errors", "leftover_dirs", "space_leak",
)


def _covariates(campaign: Campaign, drive_id: str) -> dict:
    unit = campaign.inventory[drive_id]
    return {"group": unit.group.code, "brand": unit.brand,
            "capacity_gb": unit.capacity_gb, "housing": unit.group.housing}


def cycles_frame(campaign: Campaign) -> pd.DataFrame:
    """All cycle rows of the campaign with numeric columns and drive covariates."""
    frames = []
    for drive_id in drive_ids(campaign.run_dir):
        if drive_id not in campaign.inventory:
            continue
        rows = DriveLog(campaign.run_dir, drive_id).read_cycles()
        if rows:
            frame = pd.DataFrame(rows).assign(**_covariates(campaign, drive_id))
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for col in NUMERIC_CYCLE_COLUMNS:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values(["drive_id", "cycle"], ignore_index=True)


def events_frame(campaign: Campaign) -> pd.DataFrame:
    rows = []
    for drive_id in drive_ids(campaign.run_dir):
        for e in DriveLog(campaign.run_dir, drive_id).read_events():
            rows.append({"drive_id": e.drive_id, "cycle": e.cycle, "type": e.type.value,
                         "code": e.code, "time": e.time, **{f"detail.{k}": v for k, v in
                                                             e.detail.items()
                                                             if isinstance(v, (str, int, float))}})
    return pd.DataFrame(rows)


def _drive_failures(log: DriveLog) -> tuple[list[int], int | None, str]:
    """(drive-attributed cycle-failure cycles, hard-failure cycle, hard-failure code)."""
    events = log.read_events()
    port_caused = {int(e.detail.get("failure_cycle", -1)) for e in events
                   if e.type is EventType.PORT_FAULT}
    failures = sorted(e.cycle for e in events
                      if e.type is EventType.CYCLE_FAILURE and e.cycle not in port_caused)
    hard = [(e.cycle, e.code) for e in events if e.type is EventType.HARD_FAILURE]
    return failures, (hard[0][0] if hard else None), (hard[0][1] if hard else "")


def life_table(campaign: Campaign) -> pd.DataFrame:
    """One row per drive: failure times, censoring flags and covariates."""
    rows = []
    for drive_id in campaign.assignments:
        log = DriveLog(campaign.run_dir, drive_id)
        state = log.load_state()
        failures, hard, code = _drive_failures(log)
        observed = state.cycles_done
        rows.append({
            "drive_id": drive_id,
            **_covariates(campaign, drive_id),
            "cycles": observed,
            "bytes_written": state.bytes_written,
            "t_first": failures[0] if failures else observed,
            "first_failed": bool(failures),
            "t_hard": hard if hard is not None else observed,
            "hard_failed": hard is not None,
            "hard_code": code,
            "soft_failures": len(failures),
            "status": state.status.value,
        })
    return pd.DataFrame(rows)


def recurrent_units(campaign: Campaign) -> dict[str, list[tuple[list[int], int]]]:
    """Per group: (cycle-failure cycles, last observed cycle) for every drive (for the MCF)."""
    units: dict[str, list[tuple[list[int], int]]] = {}
    for drive_id in campaign.assignments:
        log = DriveLog(campaign.run_dir, drive_id)
        failures, _, _ = _drive_failures(log)
        group = campaign.inventory[drive_id].group.code
        units.setdefault(group, []).append((failures, log.load_state().cycles_done))
    return units
