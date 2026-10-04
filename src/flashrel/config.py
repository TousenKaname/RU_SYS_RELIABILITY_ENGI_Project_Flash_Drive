"""Campaign configuration: workloads, cycle settings, failure rules, stop rule.

A *campaign* is one test run over a set of drives (``configs/campaign.yaml``).
Everything that defines the experiment lives in one YAML file so that the
harness, the analysis and the report tables read the same numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from flashrel.inventory import Inventory, load_inventory
from flashrel.units import MiB, parse_size


class ConfigError(ValueError):
    """Raised when a campaign file is missing data or inconsistent."""


@dataclass(frozen=True)
class WorkloadSpec:
    """A file-size class. File sizes are drawn log-uniformly in [min_size, max_size]."""

    name: str
    min_size: int
    max_size: int
    files_per_dir: int = 256

    def __post_init__(self) -> None:
        if not 64 <= self.min_size <= self.max_size:
            raise ConfigError(f"workload {self.name!r}: need 64 B <= min_size <= max_size")
        if self.files_per_dir < 1:
            raise ConfigError(f"workload {self.name!r}: files_per_dir must be >= 1")


@dataclass(frozen=True)
class CycleSpec:
    """How one copy-verify-delete cycle is executed."""

    fill_fraction: float = 0.90
    reserve_bytes: int = 64 * MiB
    chunk_size: int = 1 * MiB
    fsync: str = "per_file"
    readback: str = "uncached"
    retries: int = 2
    retry_backoff_s: float = 5.0

    def __post_init__(self) -> None:
        if not 0.0 < self.fill_fraction <= 0.99:
            raise ConfigError("cycle.fill_fraction must be in (0, 0.99]")
        if self.chunk_size % 4096:
            raise ConfigError("cycle.chunk_size must be a multiple of 4 KiB (direct I/O)")
        if self.fsync not in ("per_file", "per_phase"):
            raise ConfigError("cycle.fsync must be 'per_file' or 'per_phase'")
        if self.readback not in ("uncached", "buffered"):
            raise ConfigError("cycle.readback must be 'uncached' or 'buffered'")
        if self.retries < 0:
            raise ConfigError("cycle.retries must be >= 0")


@dataclass(frozen=True)
class FailurePolicy:
    """Rules that turn cycle records into failure and degradation events."""

    baseline_cycles: int = 2
    slow_fraction: float = 0.5
    slow_window: int = 3
    reconnect_timeout_s: float = 120.0
    hang_timeout_s: float = 600.0
    intermittent_limit: int = 3
    intermittent_window: int = 10

    def __post_init__(self) -> None:
        if not 0.0 < self.slow_fraction < 1.0:
            raise ConfigError("failure.slow_fraction must be in (0, 1)")
        if self.hang_timeout_s <= 0 or self.reconnect_timeout_s <= 0:
            raise ConfigError("failure timeouts must be positive")
        if min(self.baseline_cycles, self.slow_window, self.intermittent_limit) < 1:
            raise ConfigError("failure counts must be >= 1")
        if self.intermittent_window < self.intermittent_limit:
            raise ConfigError("failure.intermittent_window must be >= intermittent_limit")


@dataclass(frozen=True)
class StopRule:
    """Type I (time) and cycle-count censoring limits; either may be omitted."""

    max_cycles: int | None = None
    end_time: datetime | None = None

    def reached(self, cycles_done: int, now: datetime) -> bool:
        if self.max_cycles is not None and cycles_done >= self.max_cycles:
            return True
        return self.end_time is not None and now >= self.end_time


@dataclass(frozen=True)
class Assignment:
    """Where a drive is plugged in. Port labels are written on the hubs."""

    drive_id: str
    host: str
    port: str


@dataclass(frozen=True)
class Campaign:
    name: str
    seed: int
    data_dir: Path
    inventory: Inventory
    cycle: CycleSpec
    workloads: Mapping[str, WorkloadSpec]
    rotation: tuple[str, ...]
    failure: FailurePolicy
    stop: StopRule
    assignments: Mapping[str, Assignment]
    source: Path = field(default=Path("."))

    def workload_for_cycle(self, cycle: int) -> WorkloadSpec:
        """Workload of 1-based cycle number ``cycle`` under the rotation."""
        if cycle < 1:
            raise ValueError("cycles are numbered from 1")
        return self.workloads[self.rotation[(cycle - 1) % len(self.rotation)]]

    def drives_on(self, host: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.host == host]

    @property
    def run_dir(self) -> Path:
        """Directory that holds this campaign's logs."""
        return self.data_dir / self.name


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"section {key!r} must be a mapping")
    return value


def _end_time(value: Any) -> datetime | None:
    """Stop time as a naive local date-time (the harness compares with local time)."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value))
        except ValueError:
            raise ConfigError(f"stop.end_time is not an ISO date-time: {value!r}") from None
    if value.tzinfo is not None:  # e.g. "2026-12-01T14:00:00Z" -> local wall-clock time
        value = value.astimezone().replace(tzinfo=None)
    return value


def load_campaign(path: str | Path) -> Campaign:
    """Read and validate a campaign YAML file."""
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    base = path.parent
    try:
        name = str(raw["campaign"])
        seed = int(raw["seed"])
        inventory = load_inventory(base / raw.get("inventory", "inventory.yaml"))
    except KeyError as exc:
        raise ConfigError(f"{path}: missing top-level key {exc}") from None

    c = _section(raw, "cycle")
    cycle = CycleSpec(
        fill_fraction=float(c.get("fill_fraction", 0.90)),
        reserve_bytes=parse_size(c.get("reserve", "64MiB")),
        chunk_size=parse_size(c.get("chunk_size", "1MiB")),
        fsync=str(c.get("fsync", "per_file")),
        readback=str(c.get("readback", "uncached")),
        retries=int(c.get("retries", 2)),
        retry_backoff_s=float(c.get("retry_backoff_s", 5.0)),
    )

    workloads = {
        wname: WorkloadSpec(
            name=wname,
            min_size=parse_size(w["min_size"]),
            max_size=parse_size(w["max_size"]),
            files_per_dir=int(w.get("files_per_dir", 256)),
        )
        for wname, w in _section(raw, "workloads").items()
    }
    if not workloads:
        raise ConfigError(f"{path}: no workloads defined")
    rotation = tuple(raw.get("rotation") or workloads)
    unknown = sorted(set(rotation) - set(workloads))
    if unknown:
        raise ConfigError(f"{path}: rotation names undefined workloads {unknown}")

    f = _section(raw, "failure")
    defaults = FailurePolicy()
    unknown = sorted(set(f) - set(FailurePolicy.__dataclass_fields__))
    if unknown:
        raise ConfigError(f"{path}: unknown failure settings {unknown}")
    failure = FailurePolicy(**{k: type(getattr(defaults, k))(v) for k, v in f.items()})

    s = _section(raw, "stop")
    stop = StopRule(
        max_cycles=int(s["max_cycles"]) if s.get("max_cycles") is not None else None,
        end_time=_end_time(s.get("end_time")),
    )

    assignments: dict[str, Assignment] = {}
    for drive_id, a in _section(raw, "assignments").items():
        if drive_id not in inventory:
            raise ConfigError(f"{path}: assignment for {drive_id!r}, which is not in the inventory")
        assignments[drive_id] = Assignment(drive_id, host=str(a["host"]), port=str(a["port"]))
    if not assignments:
        raise ConfigError(f"{path}: no drives assigned")

    return Campaign(
        name=name,
        seed=seed,
        data_dir=(base / raw.get("data_dir", "../data")).resolve(),
        inventory=inventory,
        cycle=cycle,
        workloads=workloads,
        rotation=rotation,
        failure=failure,
        stop=stop,
        assignments=assignments,
        source=path,
    )
