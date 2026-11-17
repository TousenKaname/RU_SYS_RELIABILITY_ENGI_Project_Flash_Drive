"""Failure and degradation taxonomy, drive state, and the rules that turn cycles into events.

Failure modes (a cycle that ends in one of these is a *cycle failure*)::

    F1  data corruption       bytes read back differ from the bytes written (also a file at
                              rest: the identity or sentinel file), or files are missing
    F2  I/O error             a read or write error that retries do not clear
    F3  disconnect or hang    the drive leaves the bus, or makes no I/O progress for 10 min
    F4  read-only lock        the drive refuses writes (typical end-of-life behaviour)
    F5  port or hub fault     traced to the hub, the port or the computer; NOT counted
    F6  file-system fault     damaged file-system records, space not returned, capacity change

Degradation indicators (the drive still works)::

    D1  slowdown              write throughput below 50 % of the drive's own baseline
    D2  intermittent failure  a cycle failure after which the drive passes the recovery check
    D3  transient error       an error or mismatch that a retry within the cycle clears

Life data: ``first_failure`` is the first cycle failure (T); ``end_of_life`` is when the
drive is retired (it fails the recovery check, locks read-only, loses its identity, or
has 3 cycle failures within 10 cycles). Drives still working at the stop are censored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from statistics import median
from typing import Any

from flashrel.config import FailurePolicy


class Outcome(str, Enum):
    PASS = "pass"
    CORRUPTION = "corruption"      # F1
    IO_ERROR = "io_error"          # F2
    DISCONNECTED = "disconnected"  # F3
    HANG = "hang"                  # F3
    READ_ONLY = "read_only"        # F4
    HUB_FAULT = "hub_fault"        # F5 (not counted)
    FS_FAULT = "fs_fault"          # F6
    ABORTED = "aborted"            # stopped (stop time, operator, controller); not counted
    INTERRUPTED = "interrupted"    # the worker or host died mid-cycle; not counted

    @property
    def is_failure(self) -> bool:
        return self not in (Outcome.PASS, Outcome.ABORTED, Outcome.INTERRUPTED, Outcome.HUB_FAULT)


CODES = {Outcome.CORRUPTION: "F1", Outcome.IO_ERROR: "F2", Outcome.DISCONNECTED: "F3",
         Outcome.HANG: "F3", Outcome.READ_ONLY: "F4", Outcome.HUB_FAULT: "F5",
         Outcome.FS_FAULT: "F6"}
FROM_CODE = {"F1": Outcome.CORRUPTION, "F2": Outcome.IO_ERROR, "F3": Outcome.DISCONNECTED,
             "F4": Outcome.READ_ONLY, "F5": Outcome.HUB_FAULT, "F6": Outcome.FS_FAULT}


class Status(str, Enum):
    ENROLLED = "enrolled"      # intake done, test not started
    ACTIVE = "active"          # cycling
    RECOVERING = "recovering"  # running the recovery protocol after a cycle failure
    PAUSED = "paused"          # waiting out a hub event (F5)
    ATTENTION = "attention"    # automatic recovery failed; decided by the post-test protocol
    FAILED = "failed"          # end of life (retired)
    CENSORED = "censored"      # still working at the stop time


FINAL = {Status.ATTENTION.value, Status.FAILED.value, Status.CENSORED.value}


@dataclass
class Dose:
    """What a drive has been put through (the possible time scales of its life)."""

    bytes_written: int = 0
    bytes_read: int = 0
    files_created: int = 0
    files_deleted: int = 0
    dirs_created: int = 0
    dirs_deleted: int = 0
    fsyncs: int = 0

    def add(self, other: Dose) -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def copy(self) -> Dose:
        return Dose(**asdict(self))

    @property
    def file_ops(self) -> int:
        return self.files_created + self.files_deleted

    def to_json(self) -> dict[str, int]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> Dose:
        d = d or {}
        return cls(**{f.name: int(d.get(f.name, 0) or 0) for f in fields(cls)})


@dataclass
class DriveState:
    drive_id: str
    status: str = Status.ACTIVE.value
    cycles_done: int = 0
    in_progress: dict[str, Any] | None = None
    cum: Dose = field(default_factory=Dose)
    failure_cycles: list[int] = field(default_factory=list)
    first_failure: dict[str, Any] | None = None
    end_of_life: dict[str, Any] | None = None
    baseline_mbps: dict[str, float] = field(default_factory=dict)
    baseline_samples: dict[str, list[float]] = field(default_factory=dict)
    recent_mbps: dict[str, list[float]] = field(default_factory=dict)
    slow_flagged: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    sentinel_skip: list[str] = field(default_factory=list)  # damage already on record
    pending_recovery: dict[str, Any] | None = None  # a recovery protocol still to finish
    recoveries: int = 0
    transients: int = 0
    started_at: str | None = None
    censored_at: str | None = None
    last_outcome: str = ""
    last_mbps: dict[str, dict[str, float]] = field(default_factory=dict)
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["cum"] = self.cum.to_json()
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> DriveState:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["cum"] = Dose.from_json(d.get("cum"))
        return cls(**data)


def track_throughput(state: DriveState, workload: str, mbps: float,
                     policy: FailurePolicy) -> dict[str, Any] | None:
    """Maintain the per-workload baseline; return the details of the first D1 crossing."""
    samples = state.baseline_samples.setdefault(workload, [])
    if workload not in state.baseline_mbps:
        samples.append(mbps)
        if len(samples) >= policy.baseline_cycles:
            state.baseline_mbps[workload] = median(samples)
        return None
    recent = state.recent_mbps.setdefault(workload, [])
    recent.append(mbps)
    del recent[: -policy.slow_window]
    baseline = state.baseline_mbps[workload]
    threshold = policy.slow_fraction * baseline
    if (len(recent) == policy.slow_window and max(recent) < threshold
            and workload not in state.slow_flagged):
        state.slow_flagged.append(workload)
        return {"workload": workload, "baseline_mbps": round(baseline, 3),
                "threshold_mbps": round(threshold, 3), "recent_mbps": [round(x, 3) for x in recent]}
    return None


def limit_reached(state: DriveState, policy: FailurePolicy) -> bool:
    """True when cycle failures recur often enough to end the drive's life."""
    first = state.cycles_done - policy.intermittent_window + 1
    return sum(1 for c in state.failure_cycles if c >= first) >= policy.intermittent_limit


def life_record(cycle: int, code: str, when: str, dose: Dose, hours: float | None,
                **extra: Any) -> dict[str, Any]:
    return {"cycle": cycle, "code": code, "time": when, "hours_on_test": hours,
            "dose": dose.to_json(), **extra}
