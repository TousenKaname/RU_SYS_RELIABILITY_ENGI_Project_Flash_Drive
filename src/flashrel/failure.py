"""Failure and degradation taxonomy, and the rules that turn cycles into events.

Failure modes (a cycle that ends in one of these is a *cycle failure*)::

    F1  data corruption       bytes read back differ from the bytes written
    F2  I/O error             a read or write error that retries do not clear
    F3  disconnection         the drive vanishes from the host or does not enumerate
    F4  read-only lock        the drive refuses writes (typical end-of-life behaviour)
    F5  port or host fault    the failure is traced to the hub, port or computer
    F6  file-system fault     volume damage, space not reclaimed, capacity change

Degradation indicators (the drive still works)::

    D1  slowdown              write throughput below a fraction of its own baseline
    D2  intermittent failure  a cycle failure that clears after the recovery protocol
    D3  transient error       an error or mismatch that clears on retry within a cycle

A drive-attributed failure is *soft* when the drive passes the recovery check
afterwards (it is logged as D2 and the drive keeps cycling) and *hard* when it
does not, or when soft failures recur too often. The cycle of the hard failure
is the drive's failure time T; drives still running at the end are censored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from statistics import median
from typing import Any

from flashrel.config import FailurePolicy
from flashrel.cycle import CycleResult, Outcome

FAILURE_CODES: dict[Outcome, str] = {
    Outcome.CORRUPTION: "F1",
    Outcome.IO_ERROR: "F2",
    Outcome.DISCONNECTED: "F3",
    Outcome.HANG: "F3",
    Outcome.READ_ONLY: "F4",
    Outcome.FS_FAULT: "F6",
}
PORT_FAULT_CODE = "F5"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EventType(str, Enum):
    CYCLE_FAILURE = "cycle_failure"  # a cycle ended in F1-F4 or F6
    RECOVERED = "recovered"          # the drive passed the recovery check: soft failure (D2)
    ATTENTION = "attention"          # automatic recovery failed; an operator must act
    PORT_FAULT = "port_fault"        # an earlier cycle failure re-attributed to the port (F5)
    HARD_FAILURE = "hard_failure"    # end of life; the drive is retired
    TRANSIENT = "transient"          # D3
    SLOW = "slow"                    # D1
    CENSORED = "censored"            # stop rule reached while the drive still works
    NOTE = "note"                    # operator note (port move, re-plug, observation)


class DriveStatus(str, Enum):
    ACTIVE = "active"
    ATTENTION = "attention"
    FAILED = "failed"
    CENSORED = "censored"


@dataclass(frozen=True)
class Event:
    drive_id: str
    cycle: int
    type: EventType
    code: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    time: str = field(default_factory=utc_now)

    def to_json(self) -> dict[str, Any]:
        record = asdict(self)
        record["type"] = self.type.value
        return record

    @classmethod
    def from_json(cls, record: dict[str, Any]) -> Event:
        return cls(drive_id=record["drive_id"], cycle=int(record["cycle"]),
                   type=EventType(record["type"]), code=record.get("code", ""),
                   detail=record.get("detail") or {}, time=record["time"])


@dataclass
class DriveState:
    """Everything a worker needs to resume a drive after a restart."""

    drive_id: str
    status: DriveStatus = DriveStatus.ACTIVE
    cycles_done: int = 0
    bytes_written: int = 0
    soft_failures: list[int] = field(default_factory=list)
    baseline_mbps: dict[str, float] = field(default_factory=dict)
    baseline_samples: dict[str, list[float]] = field(default_factory=dict)
    recent_mbps: dict[str, list[float]] = field(default_factory=dict)
    slow_flagged: list[str] = field(default_factory=list)
    host: str = ""
    port: str = ""
    updated_at: str = ""

    @property
    def next_cycle(self) -> int:
        return self.cycles_done + 1

    def to_json(self) -> dict[str, Any]:
        record = asdict(self)
        record["status"] = self.status.value
        return record

    @classmethod
    def from_json(cls, record: dict[str, Any]) -> DriveState:
        record = dict(record)
        record["status"] = DriveStatus(record.get("status", "active"))
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in record.items() if k in known})


def failure_detail(result: CycleResult, limit: int = 20) -> dict[str, Any]:
    """Compact description of a failed cycle for the event log."""
    return {
        "outcome": result.outcome.value,
        "workload": result.workload,
        "host": result.host,
        "port": result.port,
        "errors": [{"phase": e.phase, "kind": e.kind.value, "path": e.path, "detail": e.detail}
                   for e in result.errors if not e.recovered][:limit],
        "bad_files": [{"file": c.relpath, "status": c.status.value, "bytes": c.bytes_diff,
                       "bits": c.bits_diff, "kinds": sorted({d.kind.value for d in c.diffs})}
                      for c in result.bad_files[:limit]],
        "files_corrupt": result.files_corrupt,
        "files_missing": result.files_missing,
        "files_unreadable": result.unreadable_files,
        "bits_corrupt": result.bits_corrupt,
    }


def apply_cycle(state: DriveState, result: CycleResult, policy: FailurePolicy) -> list[Event]:
    """Update ``state`` with a finished cycle and return the events it produced."""
    if result.outcome is Outcome.ABORTED:
        return []
    events: list[Event] = []
    drive, cycle = result.drive_id, result.cycle
    state.cycles_done = cycle
    state.bytes_written += result.write.bytes
    state.host, state.port = result.host, result.port
    if result.transient_errors:
        events.append(Event(drive, cycle, EventType.TRANSIENT, "D3", {
            "count": result.transient_errors,
            "mismatches_cleared_on_reread": result.transient_mismatches,
            "errors": [{"phase": e.phase, "kind": e.kind.value, "attempts": e.attempts,
                        "detail": e.detail} for e in result.errors if e.recovered][:20],
        }))
    if result.outcome is Outcome.PASS:
        slow = _track_throughput(state, result.workload, result.write.mbps, policy)
        if slow is not None:
            events.append(Event(drive, cycle, EventType.SLOW, "D1", slow))
    else:
        state.soft_failures.append(cycle)
        events.append(Event(drive, cycle, EventType.CYCLE_FAILURE, FAILURE_CODES[result.outcome],
                            failure_detail(result)))
    return events


def _track_throughput(state: DriveState, workload: str, mbps: float,
                      policy: FailurePolicy) -> dict[str, Any] | None:
    """Maintain the per-workload baseline and report the first D1 crossing."""
    samples = state.baseline_samples.setdefault(workload, [])
    if workload not in state.baseline_mbps:
        samples.append(mbps)
        if len(samples) >= policy.baseline_cycles:
            state.baseline_mbps[workload] = median(samples)
        return None
    recent = state.recent_mbps.setdefault(workload, [])
    recent.append(mbps)
    del recent[:-policy.slow_window]
    baseline = state.baseline_mbps[workload]
    threshold = policy.slow_fraction * baseline
    if (len(recent) == policy.slow_window and max(recent) < threshold
            and workload not in state.slow_flagged):
        state.slow_flagged.append(workload)
        return {"workload": workload, "baseline_mbps": round(baseline, 3),
                "threshold_mbps": round(threshold, 3), "recent_mbps": [round(x, 3) for x in recent]}
    return None


def intermittent_limit_reached(state: DriveState, policy: FailurePolicy) -> bool:
    """True when soft failures recur often enough to call the drive failed."""
    first = state.cycles_done - policy.intermittent_window + 1
    return sum(1 for c in state.soft_failures if c >= first) >= policy.intermittent_limit
