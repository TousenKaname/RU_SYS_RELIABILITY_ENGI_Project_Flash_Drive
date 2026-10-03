"""Append-only logs, one folder per drive.

    <data_dir>/<campaign>/<drive_id>/cycles.csv    one row per cycle
    <data_dir>/<campaign>/<drive_id>/events.jsonl  failure, degradation and operator events
    <data_dir>/<campaign>/<drive_id>/state.json    resume point (rewritten atomically)
    <data_dir>/<campaign>/<drive_id>/intake.json   enrollment and capacity screening
    <data_dir>/<campaign>/temperature-<host>.csv  probe readings

Every append is flushed and fsync'ed, so a crash or power cut loses at most the
cycle in progress. Plain CSV and JSON Lines keep the data readable in Excel
and in any language.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from flashrel.cycle import CycleResult
from flashrel.failure import DriveState, Event, utc_now

CYCLE_FIELDS = (
    "drive_id", "cycle", "workload", "host", "port", "started_at", "ended_at", "duration_s",
    "outcome", "planned_files", "planned_bytes", "free_before", "free_after",
    "write_bytes", "write_s", "write_mbps", "write_lat_p50_s", "write_lat_p95_s",
    "write_lat_max_s", "verify_bytes", "verify_s", "verify_mbps", "verify_lat_p95_s",
    "delete_s", "files_missing", "files_corrupt", "files_unreadable", "bytes_corrupt",
    "bits_corrupt", "corruption_kinds", "transient_errors", "unrecovered_errors",
    "leftover_dirs", "space_leak", "cache_bypassed",
)
TEMPERATURE_FIELDS = ("time", "host", "probe", "celsius")


def _iso(t: datetime | None) -> str:
    return t.isoformat(timespec="seconds") if t else ""


def cycle_row(r: CycleResult) -> dict[str, Any]:
    """Flatten a :class:`CycleResult` into one CSV row."""
    return {
        "drive_id": r.drive_id, "cycle": r.cycle, "workload": r.workload, "host": r.host,
        "port": r.port, "started_at": _iso(r.started_at), "ended_at": _iso(r.ended_at),
        "duration_s": round(r.duration_s, 3), "outcome": r.outcome.value,
        "planned_files": r.planned_files, "planned_bytes": r.planned_bytes,
        "free_before": r.free_before, "free_after": r.free_after,
        "write_bytes": r.write.bytes, "write_s": round(r.write.seconds, 3),
        "write_mbps": round(r.write.mbps, 4), "write_lat_p50_s": round(r.write.latency(0.5), 5),
        "write_lat_p95_s": round(r.write.latency(0.95), 5),
        "write_lat_max_s": round(max(r.write.file_seconds, default=0.0), 5),
        "verify_bytes": r.verify.bytes, "verify_s": round(r.verify.seconds, 3),
        "verify_mbps": round(r.verify.mbps, 4),
        "verify_lat_p95_s": round(r.verify.latency(0.95), 5),
        "delete_s": round(r.delete.seconds, 3), "files_missing": r.files_missing,
        "files_corrupt": r.files_corrupt, "files_unreadable": r.unreadable_files,
        "bytes_corrupt": r.bytes_corrupt, "bits_corrupt": r.bits_corrupt,
        "corruption_kinds": ";".join(f"{k}:{v}" for k, v in sorted(r.corruption_kinds.items())),
        "transient_errors": r.transient_errors,
        "unrecovered_errors": sum(1 for e in r.errors if not e.recovered),
        "leftover_dirs": r.leftover_dirs, "space_leak": r.space_leak,
        "cache_bypassed": int(r.cache_bypassed),
    }


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _append_csv(path: Path, fields: tuple[str, ...], row: dict[str, Any]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new:
            writer.writeheader()
        writer.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class DriveLog:
    """Read and append the logs of one drive."""

    def __init__(self, run_dir: Path, drive_id: str) -> None:
        self.drive_id = drive_id
        self.dir = Path(run_dir) / drive_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cycles_path = self.dir / "cycles.csv"
        self.events_path = self.dir / "events.jsonl"
        self.state_path = self.dir / "state.json"
        self.intake_path = self.dir / "intake.json"

    def append_cycle(self, result: CycleResult) -> None:
        _append_csv(self.cycles_path, CYCLE_FIELDS, cycle_row(result))

    def append_event(self, event: Event) -> None:
        _append(self.events_path, json.dumps(event.to_json(), sort_keys=True) + "\n")

    def load_state(self) -> DriveState:
        if not self.state_path.exists():
            return DriveState(drive_id=self.drive_id)
        return DriveState.from_json(json.loads(self.state_path.read_text(encoding="utf-8")))

    def save_state(self, state: DriveState) -> None:
        state.updated_at = utc_now()
        _write_atomic(self.state_path, json.dumps(state.to_json(), indent=2, sort_keys=True))

    def save_intake(self, record: dict[str, Any]) -> Path:
        """Save the intake record; a re-enrollment never overwrites the original."""
        path = self.intake_path
        if path.exists():
            stamp = utc_now().replace(":", "").replace("-", "")
            path = self.dir / f"intake-reenroll-{stamp}.json"
        _write_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        return path

    def read_cycles(self) -> list[dict[str, str]]:
        if not self.cycles_path.exists():
            return []
        with open(self.cycles_path, encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))

    def read_events(self) -> list[Event]:
        if not self.events_path.exists():
            return []
        with open(self.events_path, encoding="utf-8") as fh:
            return [Event.from_json(json.loads(line)) for line in fh if line.strip()]


def drive_ids(run_dir: Path) -> list[str]:
    """Drives that have a log folder in this campaign."""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    return sorted(p.name for p in run_dir.iterdir() if p.is_dir() and not p.name.startswith("."))


def append_temperature(run_dir: Path, host: str, readings: dict[str, float],
                       when: str | None = None) -> None:
    path = Path(run_dir) / f"temperature-{host}.csv"
    stamp = when or utc_now()
    for probe, celsius in sorted(readings.items()):
        _append_csv(path, TEMPERATURE_FIELDS,
                    {"time": stamp, "host": host, "probe": probe, "celsius": round(celsius, 2)})


class DriveBusy(RuntimeError):
    """Another process on this host is already testing the drive."""


@contextmanager
def drive_lock(campaign: str, drive_id: str) -> Iterator[None]:
    """Hold an OS-level lock so a drive is never driven by two processes at once.

    The lock lives in the host's temp folder (the data folder may be a cloud
    sync folder, where file locks are unreliable) and is released by the OS if
    the process dies, so a crash never leaves a stale lock behind.
    """
    path = Path(tempfile.gettempdir()) / f"flashrel-{campaign}-{drive_id}.lock"
    fh = open(path, "a+")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DriveBusy(f"{drive_id} is already being tested on this host") from None
        yield
    finally:
        fh.close()
