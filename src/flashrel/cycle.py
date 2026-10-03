"""One copy-verify-delete cycle on one drive.

A cycle has three timed phases:

1. **write** (host -> drive): create every planned file and flush it;
2. **verify** (drive -> host): read every file back, bypassing the host cache,
   and compare it byte for byte with the regenerated reference stream;
3. **delete**: remove the cycle directory and check that the space came back.

Errors are retried a few times. A retry that succeeds is logged as a
*transient* error (a degradation indicator); one that keeps failing ends the
cycle with an :class:`Outcome` that names the failure mode. A content mismatch
is re-read once, which separates a transient read glitch from data that is
really wrong on the flash.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import partial
from pathlib import Path
from statistics import median
from typing import TypeVar

from flashrel.config import CycleSpec
from flashrel.payload import PAYLOAD_DIR, CyclePlan, iter_expected
from flashrel.system.directio import cache_bypass_supported, read_chunks, sync_files, write_file
from flashrel.system.errors import ErrorKind, classify_os_error, describe
from flashrel.system.volumes import VolumeUsage, is_read_only, usage
from flashrel.units import MiB, mb_per_s
from flashrel.verify import FileCheck, FileStatus, verify_file

T = TypeVar("T")

#: The verify phase gives up after this many files that cannot be read at all.
MAX_UNREADABLE_FILES = 3
#: Free space that may stay allocated after the delete phase without suspicion.
SPACE_LEAK_TOLERANCE = 16 * MiB


class Outcome(str, Enum):
    PASS = "pass"
    CORRUPTION = "corruption"      # F1: data read back differs from data written
    IO_ERROR = "io_error"          # F2: unrecoverable read or write error
    DISCONNECTED = "disconnected"  # F3: drive vanished from the host
    READ_ONLY = "read_only"        # F4: drive refuses writes
    FS_FAULT = "fs_fault"          # F6: file-system or capacity anomaly
    ABORTED = "aborted"            # stopped by the operator; not a test result

    @property
    def is_failure(self) -> bool:
        return self not in (Outcome.PASS, Outcome.ABORTED)


_FATAL = {
    ErrorKind.DISCONNECTED: Outcome.DISCONNECTED,
    ErrorKind.READ_ONLY: Outcome.READ_ONLY,
    ErrorKind.NO_SPACE: Outcome.FS_FAULT,
}


@dataclass(frozen=True)
class ErrorRecord:
    phase: str
    kind: ErrorKind
    path: str
    detail: str
    attempts: int
    recovered: bool


@dataclass
class PhaseStats:
    files: int = 0
    bytes: int = 0
    seconds: float = 0.0
    file_seconds: list[float] = field(default_factory=list)

    def add(self, n_bytes: int, seconds: float) -> None:
        self.files += 1
        self.bytes += n_bytes
        self.file_seconds.append(seconds)

    @property
    def mbps(self) -> float:
        return mb_per_s(self.bytes, self.seconds)

    def latency(self, q: float) -> float:
        """Per-file latency quantile in seconds (0 when the phase was empty)."""
        if not self.file_seconds:
            return 0.0
        if q == 0.5:
            return median(self.file_seconds)
        ordered = sorted(self.file_seconds)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


@dataclass
class CycleResult:
    drive_id: str
    cycle: int
    workload: str
    host: str
    port: str
    started_at: datetime
    ended_at: datetime | None = None
    planned_files: int = 0
    planned_bytes: int = 0
    free_before: int | None = None
    free_after: int | None = None
    leftover_dirs: int = 0
    write: PhaseStats = field(default_factory=PhaseStats)
    verify: PhaseStats = field(default_factory=PhaseStats)
    delete: PhaseStats = field(default_factory=PhaseStats)
    bad_files: list[FileCheck] = field(default_factory=list)
    unreadable_files: int = 0
    transient_mismatches: int = 0
    errors: list[ErrorRecord] = field(default_factory=list)
    outcome: Outcome = Outcome.PASS
    cache_bypassed: bool = True

    @property
    def files_missing(self) -> int:
        return sum(1 for c in self.bad_files if c.status is FileStatus.MISSING)

    @property
    def files_corrupt(self) -> int:
        return sum(1 for c in self.bad_files if c.status is FileStatus.CORRUPT)

    @property
    def bytes_corrupt(self) -> int:
        return sum(c.bytes_diff for c in self.bad_files)

    @property
    def bits_corrupt(self) -> int:
        return sum(c.bits_diff for c in self.bad_files)

    @property
    def corruption_kinds(self) -> Counter[str]:
        return Counter(d.kind.value for c in self.bad_files for d in c.diffs)

    @property
    def transient_errors(self) -> int:
        return sum(1 for e in self.errors if e.recovered) + self.transient_mismatches

    @property
    def space_leak(self) -> int:
        if self.free_before is None or self.free_after is None:
            return 0
        return max(0, self.free_before - self.free_after)

    @property
    def duration_s(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()


class CycleAborted(Exception):
    def __init__(self, outcome: Outcome, record: ErrorRecord | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.record = record


class _FileFailed(Exception):
    def __init__(self, record: ErrorRecord) -> None:
        super().__init__(record.detail)
        self.record = record


Planner = Callable[[VolumeUsage], CyclePlan]
FaultHook = Callable[[Path, CyclePlan], None]


def _ticking(chunks: Iterable[T], tick: Callable[[], None]) -> Iterator[T]:
    """Pass chunks through, reporting progress before each (the hang watchdog)."""
    for chunk in chunks:
        tick()
        yield chunk


class CycleRunner:
    """Executes one cycle. Create a new runner for every cycle."""

    def __init__(self, mount: Path, planner: Planner, spec: CycleSpec, *, drive_id: str,
                 cycle: int, workload: str, host: str = "", port: str = "",
                 stop: threading.Event | None = None, fault_hook: FaultHook | None = None,
                 progress: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.mount = mount
        self.planner = planner
        self.spec = spec
        self.stop = stop
        self.fault_hook = fault_hook
        self.tick = progress or (lambda: None)
        self.sleep = sleep
        self.uncached = spec.readback == "uncached" and cache_bypass_supported()
        self.root = mount / PAYLOAD_DIR
        self.result = CycleResult(drive_id=drive_id, cycle=cycle, workload=workload, host=host,
                                  port=port, started_at=datetime.now(timezone.utc),
                                  cache_bypassed=self.uncached)

    # -- public ---------------------------------------------------------------------------
    def run(self) -> CycleResult:
        res = self.result
        try:
            self._clear_leftovers()
            volume = self._attempt("setup", self.mount, partial(usage, self.mount))
            res.free_before = volume.free
            plan = self.planner(volume)
            res.planned_files, res.planned_bytes = len(plan.files), plan.total_bytes
            if not plan.files:
                self._abort("setup", ErrorKind.NO_SPACE, self.mount,
                            f"only {volume.free} bytes free: nothing to write", Outcome.FS_FAULT)
            cycle_dir = self.root / plan.dirname
            self._write_phase(plan, cycle_dir)
            if self.fault_hook is not None:
                self.fault_hook(cycle_dir, plan)
            self._verify_phase(plan, cycle_dir)
            self._delete_phase(plan, cycle_dir)
            res.free_after = self._attempt("delete", self.mount, partial(usage, self.mount)).free
            if res.space_leak > SPACE_LEAK_TOLERANCE:
                self._abort("delete", ErrorKind.FS_CORRUPT, self.mount,
                            f"{res.space_leak} bytes not reclaimed after delete", Outcome.FS_FAULT)
        except CycleAborted as abort:
            res.outcome = abort.outcome
        except _FileFailed as failed:
            res.outcome = Outcome.IO_ERROR
            res.errors.append(failed.record)
        finally:
            res.ended_at = datetime.now(timezone.utc)
        if res.outcome is Outcome.PASS and res.bad_files:
            res.outcome = Outcome.CORRUPTION
        if res.outcome is Outcome.PASS and res.unreadable_files:
            res.outcome = Outcome.IO_ERROR
        return res

    # -- phases ---------------------------------------------------------------------------
    def _write_phase(self, plan: CyclePlan, cycle_dir: Path) -> None:
        stats, per_file_sync = self.result.write, self.spec.fsync == "per_file"
        made: set[Path] = set()
        written: list[Path] = []
        t_phase = time.perf_counter()
        for spec in plan.files:
            self._check_stop()
            path = cycle_dir / spec.relpath
            if path.parent not in made:
                self._attempt("write", path.parent, partial(path.parent.mkdir, parents=True,
                                                            exist_ok=True))
                made.add(path.parent)
            def write_one(spec=spec, path=path) -> int:
                # A fresh stream on every attempt, so a retry rewrites the whole file.
                stream = _ticking(iter_expected(spec, plan.cycle, plan.chunk_size), self.tick)
                return write_file(path, stream, sync=per_file_sync)

            t0 = time.perf_counter()
            self._attempt("write", path, write_one)
            stats.add(spec.size, time.perf_counter() - t0)
            written.append(path)
        if not per_file_sync:
            self._attempt("write", cycle_dir, partial(sync_files, written))
        stats.seconds = time.perf_counter() - t_phase

    def _verify_phase(self, plan: CyclePlan, cycle_dir: Path) -> None:
        stats, res = self.result.verify, self.result

        def reader(path: Path, chunk_size: int) -> Iterator[bytes]:
            return _ticking(read_chunks(path, chunk_size, uncached=self.uncached), self.tick)

        t_phase = time.perf_counter()
        for spec in plan.files:
            self._check_stop()
            path = cycle_dir / spec.relpath
            check_once = partial(verify_file, path, spec, cycle=plan.cycle,
                                 chunk_size=plan.chunk_size, read_chunks=reader)
            t0 = time.perf_counter()
            try:
                check = self._attempt("verify", path, check_once)
                if check.status is FileStatus.CORRUPT:
                    second = self._attempt("verify", path, check_once)
                    if second.status is FileStatus.OK:
                        res.transient_mismatches += 1
                    check = second
            except _FileFailed as failed:
                res.errors.append(failed.record)
                res.unreadable_files += 1
                if res.unreadable_files >= MAX_UNREADABLE_FILES:
                    raise CycleAborted(Outcome.IO_ERROR, failed.record) from None
                continue
            if check.status is FileStatus.MISSING and not self.mount.exists():
                self._abort("verify", ErrorKind.DISCONNECTED, path,
                            "volume disappeared during verification", Outcome.DISCONNECTED)
            stats.add(check.observed_size or 0, time.perf_counter() - t0)
            if check.status is not FileStatus.OK:
                res.bad_files.append(check)
        stats.seconds = time.perf_counter() - t_phase

    def _delete_phase(self, plan: CyclePlan, cycle_dir: Path) -> None:
        stats = self.result.delete
        t0 = time.perf_counter()
        try:
            self._attempt("delete", cycle_dir, partial(_remove_tree, cycle_dir, self.tick))
        except _FileFailed as failed:
            raise CycleAborted(Outcome.FS_FAULT, failed.record) from None
        stats.files, stats.bytes = len(plan.files), plan.total_bytes
        stats.seconds = time.perf_counter() - t0

    # -- helpers --------------------------------------------------------------------------
    def _clear_leftovers(self) -> None:
        """Remove directories left by an interrupted cycle."""
        if not self.root.exists():
            return
        leftovers = [p for p in self.root.iterdir() if p.is_dir()]
        self.result.leftover_dirs = len(leftovers)
        for path in leftovers:
            self._attempt("setup", path, partial(_remove_tree, path, self.tick))

    def _check_stop(self) -> None:
        if self.stop is not None and self.stop.is_set():
            raise CycleAborted(Outcome.ABORTED)

    def _abort(self, phase: str, kind: ErrorKind, path: Path, detail: str,
               outcome: Outcome) -> None:
        record = ErrorRecord(phase, kind, self._rel(path), detail, attempts=1, recovered=False)
        self.result.errors.append(record)
        raise CycleAborted(outcome, record)

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.mount).as_posix()
        except ValueError:
            return str(path)

    def _refine(self, kind: ErrorKind, phase: str) -> ErrorKind:
        """Use the volume's state to sharpen an ambiguous error code."""
        if not self.mount.exists():
            return ErrorKind.DISCONNECTED
        if phase in ("write", "delete") and kind in (ErrorKind.IO_ERROR, ErrorKind.OTHER):
            try:
                if is_read_only(self.mount):
                    return ErrorKind.READ_ONLY
            except OSError:
                return ErrorKind.DISCONNECTED
        return kind

    def _attempt(self, phase: str, path: Path, action: Callable[[], T]) -> T:
        """Run ``action``, retrying OS errors with a linear back-off.

        Disconnects, read-only locks and a full volume end the cycle at once.
        Other errors are retried; a retry that succeeds is logged as a transient
        error, and one that never succeeds raises :class:`_FileFailed`.
        """
        attempts = self.spec.retries + 1
        last: ErrorRecord | None = None
        for attempt in range(1, attempts + 1):
            try:
                value = action()
            except OSError as exc:
                kind = self._refine(classify_os_error(exc), phase)
                last = ErrorRecord(phase, kind, self._rel(path), describe(exc), attempt,
                                   recovered=False)
                if kind in _FATAL:
                    self.result.errors.append(last)
                    raise CycleAborted(_FATAL[kind], last) from None
                if attempt == attempts:
                    raise _FileFailed(last) from None
                self.sleep(self.spec.retry_backoff_s * attempt)
                continue
            if last is not None:
                self.result.errors.append(ErrorRecord(phase, last.kind, last.path, last.detail,
                                                      attempt, recovered=True))
            return value
        raise AssertionError("unreachable")


def _remove_tree(path: Path, tick: Callable[[], None] = lambda: None) -> None:
    """Delete a directory tree file by file (each removal counts as progress)."""
    if path.exists():
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
                tick()
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(path)
    if path.exists():
        raise OSError(f"{path} still exists after delete")


def run_cycle(mount: Path, planner: Planner, spec: CycleSpec, *, drive_id: str, cycle: int,
              workload: str, host: str = "", port: str = "",
              stop: threading.Event | None = None, fault_hook: FaultHook | None = None,
              progress: Callable[[], None] | None = None) -> CycleResult:
    """Convenience wrapper around :class:`CycleRunner`."""
    return CycleRunner(mount, planner, spec, drive_id=drive_id, cycle=cycle, workload=workload,
                       host=host, port=port, stop=stop, fault_hook=fault_hook,
                       progress=progress).run()
