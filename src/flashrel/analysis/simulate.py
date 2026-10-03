"""Synthetic campaign logs for rehearsing the analysis before real data exist.

The simulator writes ``cycles.csv``, ``events.jsonl`` and ``state.json`` in
exactly the format the harness produces, so the whole analysis pipeline
(life table, Weibull fits, regression, MCF, degradation, figures, workbook)
can be exercised end to end. Every parameter is a *made-up* rehearsal value;
nothing produced from it is a measurement.

Model per drive: a Weibull hard-failure life T; write throughput that falls as
exp[-a (c/T)^2] towards end of life; and intermittent cycle failures from a
power-law (Crow-AMSAA) process that concentrates near T.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from flashrel.analysis.planning import USABLE_FRACTION
from flashrel.config import Campaign
from flashrel.failure import DriveState, DriveStatus, Event, EventType
from flashrel.recorder import CYCLE_FIELDS, DriveLog


@dataclass(frozen=True)
class GroupTruth:
    shape: float
    scale: float
    write_mbps: Mapping[str, float]
    read_mbps: Mapping[str, float]
    wear_drop: float = 0.9          # ln-throughput lost by the time of failure
    soft_beta: float = 3.0          # power-law shape of intermittent failures
    soft_eta_fraction: float = 0.8  # their scale, as a fraction of T
    noise_sd: float = 0.04


#: Rehearsal values only (not measurements).
REHEARSAL_TRUTH: dict[str, GroupTruth] = {
    "S8": GroupTruth(2.6, 1500.0, {"small": 1.1, "medium": 6.0, "large": 6.4},
                     {"small": 7.0, "medium": 19.0, "large": 20.0}),
    "S16": GroupTruth(2.6, 1300.0, {"small": 1.1, "medium": 6.0, "large": 6.4},
                      {"small": 7.0, "medium": 19.0, "large": 20.0}),
    "A8": GroupTruth(2.0, 650.0, {"small": 0.8, "medium": 8.0, "large": 9.0},
                     {"small": 6.0, "medium": 17.0, "large": 18.0}),
    "A16": GroupTruth(2.0, 520.0, {"small": 0.8, "medium": 8.0, "large": 9.0},
                      {"small": 6.0, "medium": 17.0, "large": 18.0}),
}


def _soft_failures(rng: np.random.Generator, life: float, truth: GroupTruth, last: int) -> list[int]:
    eta = truth.soft_eta_fraction * life
    s, cycles = 0.0, set()
    while True:
        s += rng.exponential()
        c = math.ceil(eta * s ** (1 / truth.soft_beta))
        if c >= last:
            return sorted(cycles)
        cycles.add(c)


def simulate_campaign(campaign: Campaign, horizon: Mapping[str, int], *, seed: int = 1,
                      truth: Mapping[str, GroupTruth] | None = None,
                      start: datetime | None = None) -> None:
    """Write synthetic logs for every assigned drive; ``horizon`` is cycles per group."""
    truth = truth or REHEARSAL_TRUTH
    rng = np.random.default_rng(seed)
    start = start or datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
    for drive_id, assignment in campaign.assignments.items():
        unit = campaign.inventory[drive_id]
        g = truth[unit.group.code]
        life = g.scale * rng.weibull(g.shape)
        cap = int(horizon[unit.group.code])
        failed = life <= cap
        last = max(1, min(math.ceil(life), cap))
        soft = set(_soft_failures(rng, life, g, last))
        payload = int(unit.capacity_gb * 1e9 * USABLE_FRACTION * campaign.cycle.fill_fraction)
        log = DriveLog(campaign.run_dir, drive_id)
        state = DriveState(drive_id=drive_id, host=assignment.host, port=assignment.port)
        when = start
        below: dict[str, int] = {}
        rows = []
        for c in range(1, last + 1):
            workload = campaign.rotation[(c - 1) % len(campaign.rotation)]
            rel = math.exp(-g.wear_drop * (c / life) ** 2 + rng.normal(0, g.noise_sd))
            w, r = g.write_mbps[workload] * rel, g.read_mbps[workload] * math.sqrt(rel)
            seconds = payload / (w * 1e6) + payload / (r * 1e6) + 60
            outcome = "pass"
            if failed and c == last:
                outcome = "io_error"
            elif c in soft:
                outcome = "corruption"
            rows.append(_row(drive_id, c, workload, assignment, when, seconds, payload, w, r,
                             outcome, rng))
            when += timedelta(seconds=seconds)
            state.cycles_done, state.bytes_written = c, state.bytes_written + payload
            if outcome == "pass":
                below[workload] = below.get(workload, 0) + 1 if rel < 0.5 else 0
                if below[workload] == 3 and workload not in state.slow_flagged:
                    state.slow_flagged.append(workload)
                    log.append_event(Event(drive_id, c, EventType.SLOW, "D1",
                                           {"workload": workload}, when.isoformat()))
                continue
            state.soft_failures.append(c)
            code = "F2" if outcome == "io_error" else "F1"
            log.append_event(Event(drive_id, c, EventType.CYCLE_FAILURE, code,
                                   {"outcome": outcome, "workload": workload}, when.isoformat()))
            if outcome == "corruption":
                log.append_event(Event(drive_id, c, EventType.RECOVERED, "D2",
                                       {"failure_cycle": c}, when.isoformat()))
        if failed:
            log.append_event(Event(drive_id, last, EventType.HARD_FAILURE, "F2",
                                   {"reason": "simulated end of life"}, when.isoformat()))
            state.status = DriveStatus.FAILED
        else:
            log.append_event(Event(drive_id, last, EventType.CENSORED, "",
                                   {"cycles_done": last}, when.isoformat()))
            state.status = DriveStatus.CENSORED
        with open(log.cycles_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CYCLE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        log.save_state(state)


def _row(drive_id, cycle, workload, assignment, when, seconds, payload, w, r, outcome, rng):
    corrupt = int(rng.integers(1, 4)) if outcome == "corruption" else 0
    row = dict.fromkeys(CYCLE_FIELDS, 0)
    row.update({
        "drive_id": drive_id, "cycle": cycle, "workload": workload, "host": assignment.host,
        "port": assignment.port, "started_at": when.isoformat(timespec="seconds"),
        "ended_at": (when + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        "duration_s": round(seconds, 1), "outcome": outcome, "planned_bytes": payload,
        "write_bytes": payload, "write_mbps": round(w, 4), "verify_bytes": payload,
        "verify_mbps": round(r, 4), "files_corrupt": corrupt,
        "bits_corrupt": corrupt * int(rng.integers(1, 40)), "corruption_kinds": "",
        "cache_bypassed": 1,
    })
    return row
