"""Append-only records, atomically replaced state files, and process locks.

    data/<campaign>/
      campaign.json            the frozen design (written at the start)
      run.json                 campaign state: setup / running / stopped, start and stop times
      controller.json          heartbeat of the controller (workers stop if it goes stale)
      presence.json            which drives the controller sees, refreshed every few seconds
      host/                    host_events.jsonl, operator.jsonl, hub_load.csv, temperature.csv,
                               host_info.json, inventory.json, checks.json, selftest.json
      drives/<ID>/
        intake.json            enrollment record (identity, USB serial, location, baseline)
        state.json             resume point of the worker (atomically replaced)
        live.json              current phase and progress, for the dashboard
        cycles.csv             one row per completed cycle
        events.jsonl           failures, recoveries, degradation, notes
        sentinel.csv           every check of the data at rest and the file-system records
        trace.csv              progress every minute (throughput within a cycle)
        phase_hist.jsonl       per-file latency histogram of every phase
        control.json           requests from the controller (stop)
        lost.json              when the worker last saw its volume vanish (hub-event check)
        worker.log             output of the worker process

Every append is flushed and fsync'ed, so a power cut loses at most the cycle in
progress. A line torn by a power cut is isolated on its own line and skipped on read.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flashrel.timeutil import iso

CYCLE_FIELDS = (
    "campaign", "drive", "group", "hub", "port", "cycle", "workload", "started_at", "ended_at",
    "duration_s", "outcome", "code", "planned_files", "planned_bytes", "free_before", "free_after",
    "write_files", "write_bytes", "write_s", "write_mbps", "write_p50_s", "write_p90_s",
    "write_p99_s", "write_max_s", "verify_files", "verify_bytes", "verify_s", "verify_mbps",
    "verify_p50_s", "verify_p99_s", "verify_max_s", "delete_files", "delete_s", "delete_fps",
    "delete_p50_s", "delete_p99_s", "delete_max_s", "files_missing", "files_corrupt",
    "files_unreadable", "bytes_corrupt", "bits_corrupt", "corruption_kinds", "transient_errors",
    "errors", "leftover_dirs", "space_leak", "sentinel_checks", "sentinel_warnings",
    "cum_bytes_written", "cum_bytes_read", "cum_files_created", "cum_files_deleted", "cum_fdw",
    "hours_on_test", "mount", "cache_bypassed", "software",
)
SENTINEL_FIELDS = (
    "time", "drive", "cycle", "phase", "ok", "failed", "code", "reads", "elapsed_s", "identity",
    "sentinel", "boot", "upcase", "rootdir", "dirty", "warnings", "detail", "cum_bytes_written",
    "cum_files_created", "cum_files_deleted",
)
TRACE_FIELDS = ("time", "drive", "cycle", "workload", "phase", "files_done", "files_total",
                "bytes_done", "bytes_total", "rate_mbps", "rate_fps", "free_bytes")
HUB_FIELDS = ("time", "hub", "drives_active", "writing", "verifying", "deleting", "mbps_total",
              "mbps_write", "mbps_read")
TEMPERATURE_FIELDS = ("time", "source", "location", "celsius", "note")


class StoreError(RuntimeError):
    """A record file exists but cannot be read (held by another program, or damaged)."""


# -- low-level file helpers -------------------------------------------------------------------
def _retry(fn, attempts: int = 120, delay: float = 0.25):
    """Run ``fn``; wait while another program (Excel, antivirus, sync) holds the file."""
    for attempt in range(attempts):
        try:
            return fn()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def atomic_write_text(path: str | Path, text: str) -> None:
    path = str(path)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # one per writing thread
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    _retry(lambda: os.replace(tmp, path), attempts=200, delay=0.05)


def atomic_write_json(path: str | Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True, default=str))


def read_json(path: str | Path, default: Any = None, *, strict: bool = False) -> Any:
    """The parsed file, or ``default`` when it does not exist.

    A file that exists but stays unreadable (held by a scanner or a sync client, or not
    JSON) also gives ``default``, unless ``strict``: then it is retried for about 15 s and
    StoreError is raised, so a state file that is merely held is never taken for a
    missing one (and overwritten with a fresh state)."""
    attempts, delay = (60, 0.25) if strict else (20, 0.05)
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (PermissionError, ValueError) as exc:  # mid-replace or held by a scanner
            last = exc
            time.sleep(delay)
    if strict:
        raise StoreError(f"{path} exists but cannot be read: {last}")
    return default


def _cut_torn_tail(path: str) -> None:
    """A power cut can leave a partial last line. Every file has a single writer, so the
    partial line is moved to ``<file>.torn`` (kept for the record) and cut off; a partial
    CSV row could otherwise look like a complete one."""
    try:
        with open(path, "rb+") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return
            back = min(size, 1 << 20)
            fh.seek(size - back)
            tail = fh.read(back)
            cut = tail.rfind(b"\n")
            keep = size - back + cut + 1 if cut >= 0 else (0 if back == size else size)
            with open(path + ".torn", "ab") as torn:
                torn.write(tail[cut + 1:] + b"\n")
            fh.truncate(keep)
    except FileNotFoundError:
        return


_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    with _APPEND_LOCKS_GUARD:
        return _APPEND_LOCKS.setdefault(os.path.abspath(path), threading.Lock())


def append_text(path: str | Path, text: str) -> None:
    """Append and fsync. Never fails because another program holds the file.

    Excel locks a CSV it has open, and a scanner or sync client may hold a file for a
    moment. After about 2 s of retries the record goes to ``<file>.pending`` instead and
    is merged into the file, in order, by the next append that gets through, so no record
    is lost and the writer (a drive's worker, the controller loop) never stalls or crashes.
    """
    path = str(path)
    pending = path + ".pending"

    def write() -> None:
        _cut_torn_tail(path)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            if os.path.exists(pending):
                try:
                    with open(pending, encoding="utf-8", newline="") as held:
                        fh.write(held.read())
                    fh.flush()
                    os.fsync(fh.fileno())
                    os.remove(pending)
                except OSError:
                    pass  # merged next time
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

    with _path_lock(path):
        try:
            _retry(write, attempts=8, delay=0.25)
        except PermissionError:
            with open(pending, "a", encoding="utf-8", newline="") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    append_text(path, json.dumps(record, sort_keys=True, default=str) + "\n")


def append_csv(path: str | Path, fields: tuple[str, ...], row: dict[str, Any]) -> None:
    path = str(path)
    new = (not os.path.exists(path) or os.path.getsize(path) == 0) and not os.path.exists(path + ".pending")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    if new:
        writer.writeheader()
    writer.writerow({k: _cell(row.get(k)) for k in fields})
    append_text(path, buf.getvalue())


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _read_text(path: str | Path) -> str:
    """The file plus any records parked in ``<file>.pending``."""
    text = ""
    for p in (str(path), str(path) + ".pending"):
        for _ in range(20):
            try:
                with open(p, encoding="utf-8", newline="") as fh:
                    text += fh.read()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.05)
    return text


def read_csv(path: str | Path) -> list[dict[str, str]]:
    text = _read_text(path)
    if not text:
        return []
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    return [r for r in rows if None not in r and None not in r.values()]


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    out = []
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # torn by a power cut
    return out


class FileLock:
    """An OS lock on a file, released by the OS when the process dies."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:  # who holds it, for the operator (the lock itself is the open handle)
            with open(self.path + ".pid", "w") as pid:
                pid.write(f"{os.getpid()}\n")
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def held_by_other(self) -> bool:
        if self.acquire():
            self.release()
            return False
        return True


# -- campaign and drive records ---------------------------------------------------------------
class DriveStore:
    def __init__(self, campaign_dir: Path, drive_id: str) -> None:
        self.drive_id = drive_id
        self.dir = Path(campaign_dir) / "drives" / drive_id
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir
        self.cycles_csv, self.events_jsonl = p / "cycles.csv", p / "events.jsonl"
        self.sentinel_csv, self.trace_csv = p / "sentinel.csv", p / "trace.csv"
        self.hist_jsonl, self.state_json = p / "phase_hist.jsonl", p / "state.json"
        self.live_json, self.intake_json = p / "live.json", p / "intake.json"
        self.control_json, self.lock_path = p / "control.json", p / ".worker.lock"
        self.worker_log, self.lost_json = p / "worker.log", p / "lost.json"

    def event(self, type_: str, cycle: int, code: str = "", **detail: Any) -> dict[str, Any]:
        record = {"time": iso(), "drive": self.drive_id, "cycle": cycle, "type": type_,
                  "code": code, "detail": detail}
        append_jsonl(self.events_jsonl, record)
        return record

    def append_cycle(self, row: dict[str, Any]) -> None:
        append_csv(self.cycles_csv, CYCLE_FIELDS, row)

    def append_sentinel(self, row: dict[str, Any]) -> None:
        append_csv(self.sentinel_csv, SENTINEL_FIELDS, row)

    def append_trace(self, row: dict[str, Any]) -> None:
        append_csv(self.trace_csv, TRACE_FIELDS, row)

    def append_hist(self, record: dict[str, Any]) -> None:
        append_jsonl(self.hist_jsonl, record)

    def read_cycles(self) -> list[dict[str, str]]:
        return [r for r in read_csv(self.cycles_csv) if str(r.get("cycle", "")).isdigit()]

    def note(self, cycle: int, **detail: Any) -> dict[str, Any]:
        """Operator notes from the controller go to their own file (the worker owns
        events.jsonl; two processes never append to one file)."""
        record = {"time": iso(), "drive": self.drive_id, "cycle": cycle, "type": "note", "code": "",
                  "detail": detail}
        append_jsonl(self.dir / "notes.jsonl", record)
        return record

    def read_events(self) -> list[dict[str, Any]]:
        events = read_jsonl(self.events_jsonl) + read_jsonl(self.dir / "notes.jsonl")
        events.sort(key=lambda e: e.get("time", ""))
        return events

    def load_state(self, *, strict: bool = False) -> dict[str, Any] | None:
        return read_json(self.state_json, strict=strict)

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = iso()
        atomic_write_json(self.state_json, state)

    def load_intake(self) -> dict[str, Any] | None:
        return read_json(self.intake_json)

    def save_intake(self, record: dict[str, Any]) -> None:
        if self.intake_json.exists():  # a re-enrollment never overwrites the original
            stamp = iso().replace(":", "").replace("-", "").replace(".", "")[:15]
            os.replace(self.intake_json, self.dir / f"intake-previous-{stamp}.json")
        atomic_write_json(self.intake_json, record)

    def save_live(self, live: dict[str, Any]) -> None:
        atomic_write_json(self.live_json, live)

    def load_live(self) -> dict[str, Any] | None:
        return read_json(self.live_json)

    def request(self, **control: Any) -> None:
        atomic_write_json(self.control_json, {"time": iso(), **control})

    def control(self) -> dict[str, Any]:
        return read_json(self.control_json, {}) or {}

    def clear_control(self) -> None:
        try:
            os.remove(self.control_json)
        except FileNotFoundError:
            pass


class CampaignStore:
    def __init__(self, data_dir: str | Path, name: str) -> None:
        self.dir = Path(data_dir) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.host_dir = self.dir / "host"
        self.host_dir.mkdir(exist_ok=True)
        (self.dir / "drives").mkdir(exist_ok=True)
        self.campaign_json = self.dir / "campaign.json"
        self.run_json = self.dir / "run.json"
        self.controller_json = self.dir / "controller.json"
        self.presence_json = self.dir / "presence.json"
        self.lock_path = self.dir / ".controller.lock"

    def drive(self, drive_id: str) -> DriveStore:
        return DriveStore(self.dir, drive_id)

    def host_event(self, type_: str, **detail: Any) -> dict[str, Any]:
        record = {"time": iso(), "type": type_, **detail}
        append_jsonl(self.host_dir / "host_events.jsonl", record)
        return record

    def operator(self, action: str, **detail: Any) -> None:
        append_jsonl(self.host_dir / "operator.jsonl", {"time": iso(), "action": action, **detail})

    def run_state(self, *, strict: bool = False) -> dict[str, Any]:
        return read_json(self.run_json, {}, strict=strict) or {}

    def save_run_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = iso()
        atomic_write_json(self.run_json, state)
