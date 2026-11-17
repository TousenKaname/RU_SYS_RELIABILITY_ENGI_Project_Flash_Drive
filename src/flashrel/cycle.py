"""One copy -> verify -> delete cycle on one drive (the same cycle as in run 1).

1. **write** (host -> drive): create every planned file and flush it to the drive;
2. **verify** (drive -> host): read every file back around the host cache and compare it
   byte for byte with the regenerated reference stream; a file that does not match is
   read a second time (a match then is a transient mismatch, D3);
3. **delete**: remove the files and folders, then check that the free space came back.

New in run 2: the stop time is checked between chunks and files in all three phases
(run 1 did not check it in the delete phase); ``hooks.periodic`` runs the sentinel
check every few minutes in every phase and can end the cycle at the moment a failure
is found; every file operation is counted (``Dose``), so a failure carries the exact
number of bytes and files the drive had seen when it was found.
"""

from __future__ import annotations

import math
import os
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from flashrel.config import CycleSpec
from flashrel.errors import ErrorKind, classify, describe
from flashrel.failure import CODES, Dose, Outcome
from flashrel.payload import CHECK_DIR, PAYLOAD_DIR, CyclePlan, iter_expected
from flashrel.platform.base import Platform
from flashrel.timeutil import iso
from flashrel.units import MiB, mb_per_s
from flashrel.verify import FileCheck, FileStatus, verify_file

T = TypeVar("T")
MAX_UNREADABLE_FILES = 3
SPACE_LEAK_TOLERANCE = 16 * MiB
HIST_EDGES = [10 ** (k / 4) for k in range(-16, 13)]  # 0.1 ms ... 1000 s, 4 bins per decade


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

    @property
    def fps(self) -> float:
        return self.files / self.seconds if self.seconds > 0 else 0.0

    def quantile(self, q: float) -> float:
        if not self.file_seconds:
            return 0.0
        ordered = sorted(self.file_seconds)
        return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]

    def histogram(self) -> list[int]:
        counts = [0] * (len(HIST_EDGES) + 1)
        for s in self.file_seconds:
            k = 0
            while k < len(HIST_EDGES) and s >= HIST_EDGES[k]:
                k += 1
            counts[k] += 1
        return counts


@dataclass(frozen=True)
class ErrorRecord:
    phase: str
    kind: str
    path: str
    detail: str
    attempts: int
    recovered: bool


@dataclass
class CycleResult:
    drive_id: str
    cycle: int
    workload: str
    started_at: str
    ended_at: str = ""
    mount: str = ""
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
    phase: str = "setup"            # phase in which the cycle ended
    sentinel: dict[str, Any] | None = None
    sentinel_checks: int = 0
    sentinel_warnings: int = 0
    dose: Dose = field(default_factory=Dose)
    cache_bypassed: bool = True
    t0: float = field(default_factory=time.monotonic)
    duration_s: float = 0.0

    @property
    def code(self) -> str:
        return CODES.get(self.outcome, "")

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


class CycleAborted(Exception):
    def __init__(self, outcome: Outcome, record: ErrorRecord | None = None,
                 sentinel: dict[str, Any] | None = None) -> None:
        super().__init__(outcome.value)
        self.outcome = outcome
        self.record = record
        self.sentinel = sentinel


class _FileFailed(Exception):
    def __init__(self, record: ErrorRecord) -> None:
        super().__init__(record.detail)
        self.record = record


@dataclass
class Hooks:
    """Callbacks from the engine to the worker."""

    should_stop: Callable[[], bool] = lambda: False
    progress: Callable[[str, int, int], None] = lambda phase, files, nbytes: None
    phase: Callable[[str, dict[str, Any]], None] = lambda name, info: None
    periodic: Callable[[str], None] = lambda phase: None   # may raise CycleAborted
    after_write: Callable[[str, CyclePlan], None] | None = None  # self-test fault injection


Planner = Callable[[int, int], CyclePlan]  # (free bytes, cluster size) -> plan


class CycleRunner:
    """Executes one cycle. A new runner for every cycle."""

    def __init__(self, platform: Platform, root: str, planner: Planner, spec: CycleSpec, *,
                 drive_id: str, cycle: int, workload: str, hooks: Hooks | None = None,
                 quarantined: set[str] | None = None, data_dir: str = PAYLOAD_DIR,
                 clear_dirs: tuple[str, ...] = (PAYLOAD_DIR, CHECK_DIR),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.platform = platform
        self.root = root
        self.planner = planner
        self.spec = spec
        self.hooks = hooks or Hooks()
        self.quarantined = set(quarantined or ())
        self.data_dir = data_dir
        self.clear_dirs = clear_dirs
        self.sleep = sleep
        self.result = CycleResult(drive_id=drive_id, cycle=cycle, workload=workload,
                                  started_at=iso(), mount=root,
                                  cache_bypassed=spec.readback == "uncached" and platform.cache_bypass())

    # -- public -----------------------------------------------------------------------------
    def run(self) -> CycleResult:
        res = self.result
        try:
            self._phase("setup")
            self._clear_leftovers()
            total, free, cluster = self._attempt("setup", self.root, lambda: self.platform.usage(self.root))
            res.free_before = free
            plan = self.planner(free, cluster)
            res.planned_files, res.planned_bytes = len(plan.files), plan.total_bytes
            if not plan.files:
                self._abort("setup", ErrorKind.NO_SPACE, self.root,
                            f"only {free} bytes free: nothing to write", Outcome.FS_FAULT)
            cycle_dir = os.path.join(self.root, self.data_dir, plan.dirname)
            self.hooks.periodic("setup")
            self._write_phase(plan, cycle_dir)
            if self.hooks.after_write is not None:
                self.hooks.after_write(cycle_dir, plan)
            self.hooks.periodic("write-end")
            self._verify_phase(plan, cycle_dir)
            self.hooks.periodic("verify-end")
            self._delete_phase(plan, cycle_dir)
            res.free_after = self._attempt("delete", self.root, lambda: self.platform.usage(self.root))[1]
            if res.space_leak > SPACE_LEAK_TOLERANCE:
                self._abort("delete", ErrorKind.FS_CORRUPT, self.root,
                            f"{res.space_leak} bytes not returned after the delete", Outcome.FS_FAULT)
            self.hooks.periodic("delete-end")
        except CycleAborted as abort:
            res.outcome = abort.outcome
            if abort.sentinel is not None:
                res.sentinel = abort.sentinel
        except _FileFailed as failed:
            res.outcome = Outcome.FS_FAULT if failed.record.kind == ErrorKind.FS_CORRUPT.value else Outcome.IO_ERROR
            res.errors.append(failed.record)
        finally:
            res.ended_at = iso()
            res.duration_s = round(time.monotonic() - res.t0, 3)
        # A confirmed finding is the outcome of the cycle whatever ended the cycle after it:
        # the delete failing on the damaged folder (as on the 16 GB drives of run 1), a stop,
        # a disconnect or an I/O error. Wrong or missing data outranks unreadable files.
        if res.bad_files:
            if res.outcome is Outcome.ABORTED:
                res.errors.append(ErrorRecord(res.phase, "stopped", "", "the cycle was cut short by a "
                                              "stop request after the finding", 1, False))
            res.outcome = Outcome.CORRUPTION
        elif res.unreadable_files and res.outcome in (Outcome.PASS, Outcome.ABORTED):
            res.outcome = Outcome.IO_ERROR
        return res

    # -- phases -----------------------------------------------------------------------------
    def _phase(self, name: str, **info: Any) -> None:
        self.result.phase = name
        self.hooks.phase(name, info)

    def _write_phase(self, plan: CyclePlan, cycle_dir: str) -> None:
        res, stats = self.result, self.result.write
        self._phase("write", files_total=len(plan.files), bytes_total=plan.total_bytes)
        made: set[str] = set()
        t_phase = time.perf_counter()
        sync = self.spec.fsync == "per_file"
        for spec in plan.files:
            self._check_stop()
            path = os.path.join(cycle_dir, *spec.relpath.split("/"))
            parent = os.path.dirname(path)
            if parent not in made:
                self._attempt("write", parent, lambda p=parent: self.platform.make_dirs(p))
                res.dose.dirs_created += 1
                made.add(parent)

            def write_one(spec=spec, path=path) -> int:
                return self.platform.write_file(path, self._stream(spec, plan), sync=sync)

            t0 = time.perf_counter()
            self._attempt("write", path, write_one)
            stats.add(spec.size, time.perf_counter() - t0)
            res.dose.files_created += 1
            res.dose.fsyncs += 1 if sync else 0
            self.hooks.progress("write", 1, 0)
            self.hooks.periodic("write")
        stats.seconds = time.perf_counter() - t_phase

    def _stream(self, spec, plan: CyclePlan) -> Iterator[bytes]:
        """Expected chunks of a file; checks the stop flag and reports progress per chunk.
        A retry draws a fresh stream, so a retried file is rewritten from the start."""
        for chunk in iter_expected(spec, plan.cycle, plan.chunk_size):
            self._check_stop()
            yield chunk
            self.result.dose.bytes_written += len(chunk)
            self.hooks.progress("write", 0, len(chunk))

    def _verify_phase(self, plan: CyclePlan, cycle_dir: str) -> None:
        res, stats = self.result, self.result.verify
        self._phase("verify", files_total=len(plan.files), bytes_total=plan.total_bytes)
        uncached = self.result.cache_bypassed

        def reader(path: str, chunk_size: int) -> Iterator[bytes]:
            for chunk in self.platform.read_chunks(path, chunk_size, uncached=uncached):
                self._check_stop()
                res.dose.bytes_read += len(chunk)
                self.hooks.progress("verify", 0, len(chunk))
                yield chunk

        t_phase = time.perf_counter()
        for spec in plan.files:
            self._check_stop()
            path = os.path.join(cycle_dir, *spec.relpath.split("/"))

            def check_once(path=path, spec=spec) -> FileCheck:
                return verify_file(path, spec, cycle=plan.cycle, chunk_size=plan.chunk_size,
                                   read_chunks=reader)

            t0 = time.perf_counter()
            try:
                check = self._attempt("verify", path, check_once)
                if check.status is FileStatus.CORRUPT:
                    try:
                        second = self._attempt("verify", path, check_once)
                    except _FileFailed as failed:  # a re-read that fails does not clear the mismatch
                        res.errors.append(failed.record)
                    else:
                        if second.status is FileStatus.OK:
                            res.transient_mismatches += 1
                        check = second
            except _FileFailed as failed:
                res.errors.append(failed.record)
                res.unreadable_files += 1
                if res.unreadable_files >= MAX_UNREADABLE_FILES:
                    raise CycleAborted(Outcome.IO_ERROR, failed.record) from None
                continue
            if check.status is FileStatus.MISSING and not self.platform.present(self.root):
                self._abort("verify", ErrorKind.DISCONNECTED, path,
                            "volume disappeared during the read-back", Outcome.DISCONNECTED)
            stats.add(check.observed_size or 0, time.perf_counter() - t0)
            if check.status is not FileStatus.OK:
                res.bad_files.append(check)
            self.hooks.progress("verify", 1, 0)
            self.hooks.periodic("verify")
        stats.seconds = time.perf_counter() - t_phase

    def _delete_phase(self, plan: CyclePlan, cycle_dir: str) -> None:
        res, stats = self.result, self.result.delete
        self._phase("delete", files_total=len(plan.files), bytes_total=plan.total_bytes)
        missing = {c.relpath for c in res.bad_files if c.status is FileStatus.MISSING}
        by_dir: dict[str, list] = {}
        for spec in plan.files:
            by_dir.setdefault(spec.relpath.rsplit("/", 1)[0], []).append(spec)
        t_phase = time.perf_counter()
        for dirname, specs in by_dir.items():
            for spec in specs:
                self._check_stop()
                if spec.relpath in missing:
                    continue
                path = os.path.join(cycle_dir, *spec.relpath.split("/"))
                t0 = time.perf_counter()
                if self._remove_planned(path):
                    stats.add(spec.size, time.perf_counter() - t0)
                    res.dose.files_deleted += 1
                else:  # read back correctly, gone now: its directory entry was lost (F1)
                    res.bad_files.append(FileCheck(spec.relpath, FileStatus.MISSING, spec.size, None))
                self.hooks.progress("delete", 1, 0)
                self.hooks.periodic("delete")
            self._remove_dir(os.path.join(cycle_dir, *dirname.split("/")))
        self._remove_dir(cycle_dir)
        stats.seconds = time.perf_counter() - t_phase

    def _remove_planned(self, path: str) -> bool:
        """Delete a file the read-back has just seen. False when the file is not there at
        the first try: that is a missing file, not a delete. Not found after an earlier
        attempt failed is accepted (that attempt may have removed it before failing)."""
        tried = False

        def remove() -> bool:
            nonlocal tried
            try:
                self.platform.remove_file(path)
                return True
            except FileNotFoundError:
                if not self.platform.present(self.root):
                    raise OSError(0, "volume disappeared", path, 1167) from None
                return tried
            except OSError:
                tried = True
                raise

        return self._attempt("delete", path, remove)

    def _remove_if_present(self, path: str) -> None:
        """Leftovers of an interrupted cycle: whatever is there goes, absence is fine."""
        try:
            self.platform.remove_file(path)
        except FileNotFoundError:
            if not self.platform.present(self.root):
                raise OSError(0, "volume disappeared", path, 1167) from None

    def _remove_dir(self, path: str) -> None:
        def rm() -> None:
            try:
                self.platform.remove_dir(path)
            except FileNotFoundError:
                if not self.platform.present(self.root):
                    raise OSError(0, "volume disappeared", path, 1167) from None
        try:
            self._attempt("delete", path, rm)
        except _FileFailed as failed:
            raise CycleAborted(Outcome.FS_FAULT, failed.record) from None
        self.result.dose.dirs_deleted += 1

    # -- helpers ----------------------------------------------------------------------------
    def _clear_leftovers(self) -> None:
        """Remove what an interrupted or failed cycle left behind (never a quarantined folder)."""
        for top in self.clear_dirs:
            base = os.path.join(self.root, top)
            names = self._attempt("setup", base, lambda b=base: sorted(os.listdir(b)) if os.path.isdir(b) else [])
            for name in names:
                rel = f"{top}/{name}"
                if rel in self.quarantined:
                    continue
                self.result.leftover_dirs += 1
                self._remove_tree(os.path.join(base, name))

    def _remove_tree(self, path: str) -> None:
        if os.path.isfile(path):
            self._attempt("setup", path, lambda: self.platform.remove_file(path))
            self.result.dose.files_deleted += 1
            return
        for base, dirs, files in os.walk(path, topdown=False):
            for name in files:
                self._check_stop()
                full = os.path.join(base, name)
                self._attempt("setup", full, lambda f=full: self._remove_if_present(f))
                self.result.dose.files_deleted += 1
                self.hooks.progress("setup", 1, 0)
            for name in dirs:
                full = os.path.join(base, name)
                self._attempt("setup", full, lambda d=full: self.platform.remove_dir(d))
                self.result.dose.dirs_deleted += 1
        self._attempt("setup", path, lambda: self.platform.remove_dir(path))
        self.result.dose.dirs_deleted += 1

    def _check_stop(self) -> None:
        if self.hooks.should_stop():
            raise CycleAborted(Outcome.ABORTED)

    def _abort(self, phase: str, kind: ErrorKind, path: str, detail: str, outcome: Outcome) -> None:
        record = ErrorRecord(phase, kind.value, self._rel(path), detail, 1, False)
        self.result.errors.append(record)
        raise CycleAborted(outcome, record)

    def _rel(self, path: str) -> str:
        try:
            return os.path.relpath(path, self.root).replace(os.sep, "/")
        except ValueError:
            return path

    def _refine(self, kind: ErrorKind, phase: str) -> ErrorKind:
        """Sharpen an ambiguous error from the state of the volume."""
        if not self.platform.present(self.root):
            return ErrorKind.DISCONNECTED
        if phase in ("write", "delete", "setup") and kind in (ErrorKind.IO_ERROR, ErrorKind.OTHER):
            try:
                if self.platform.is_read_only(self.root):
                    return ErrorKind.READ_ONLY
            except OSError:
                return ErrorKind.DISCONNECTED
        return kind

    def _attempt(self, phase: str, path: str, action: Callable[[], T]) -> T:
        """Run ``action``, retrying OS errors with a linear back-off (5 s, 10 s).

        A disconnect, a read-only lock or a full volume ends the cycle at once. A retry
        that succeeds is logged as a transient error (D3). A stop during the back-off ends
        the cycle as stopped (not counted): the remaining attempts are never rushed.
        """
        attempts = self.spec.retries + 1
        last: ErrorRecord | None = None
        fatal = {ErrorKind.DISCONNECTED: Outcome.DISCONNECTED, ErrorKind.READ_ONLY: Outcome.READ_ONLY,
                 ErrorKind.NO_SPACE: Outcome.FS_FAULT}
        for attempt in range(1, attempts + 1):
            try:
                value = action()
            except OSError as exc:
                kind = self._refine(classify(exc), phase)
                last = ErrorRecord(phase, kind.value, self._rel(path), describe(exc), attempt, False)
                if kind in fatal:
                    self.result.errors.append(last)
                    raise CycleAborted(fatal[kind], last) from None
                if attempt == attempts:
                    raise _FileFailed(last) from None
                self.sleep(self.spec.retry_backoff_s * attempt)
                if self.hooks.should_stop():
                    self.result.errors.append(ErrorRecord(phase, last.kind, last.path, last.detail
                                                          + " (retries cut short by a stop)", attempt, False))
                    raise CycleAborted(Outcome.ABORTED, last) from None
                continue
            if last is not None:
                self.result.errors.append(ErrorRecord(phase, last.kind, last.path, last.detail,
                                                      attempt, True))
            return value
        raise AssertionError("unreachable")
