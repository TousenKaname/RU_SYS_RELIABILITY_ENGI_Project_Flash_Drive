"""Run many drives in parallel: one worker process per drive, one supervisor per host.

Each worker loops over cycles for its drive until the drive fails, needs an
operator, or the campaign's stop rule censors it. Separate processes keep a
hung or crashing drive from stalling the others. The logs are the source of
truth: a worker saves its state right after logging a cycle and, on start,
reconciles that state with the logs, so no cycle is ever counted twice.

After a cycle failure the worker runs the automatic part of the recovery
protocol: wait for the drive to re-enumerate, check that it still accepts
writes, and run a short recovery check (64 MiB written, verified, deleted,
with fresh data every time). A drive that passes keeps cycling, and the
failure is logged as intermittent (D2). A drive that does not is set to
*attention* for an operator (``flashrel recheck`` / ``flashrel retire``).

The supervisor also runs a watchdog: every worker reports progress after each
1 MiB chunk, and a drive that makes no progress for ``hang_timeout_s`` is
logged as hung (F3, the "blocking" failure seen by Boboila and Desnoyers),
its worker is stopped, and the drive is set to *attention*. A drive that is
missing at the start of a cycle is logged as an F3 cycle failure as well.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import signal
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flashrel.config import Campaign, WorkloadSpec, load_campaign
from flashrel.cycle import CycleResult, ErrorRecord, Outcome, Planner, run_cycle
from flashrel.failure import (
    FAILURE_CODES,
    DriveState,
    DriveStatus,
    Event,
    EventType,
    apply_cycle,
    intermittent_limit_reached,
)
from flashrel.payload import fill_budget, plan_cycle
from flashrel.recorder import DriveLog, append_temperature, drive_lock
from flashrel.sensors import NullTemperatureSource, TemperatureSource
from flashrel.system.errors import ErrorKind
from flashrel.system.power import keep_awake
from flashrel.system.volumes import VolumeUsage, is_read_only, wait_for_drive
from flashrel.units import KiB, MiB

#: Size and file mix of the short check run after a failure and by ``recheck``.
CHECK_BUDGET = 64 * MiB
CHECK_WORKLOAD = WorkloadSpec("check", min_size=64 * KiB, max_size=8 * MiB, files_per_dir=64)
#: Planning never assumes clusters smaller than exFAT's default for 8-32 GB volumes,
#: so a misreported cluster size can only under-fill a drive, never overfill it.
MIN_PLANNING_CLUSTER = 32 * KiB
#: A supervisor loop that stalls this long means the host slept: reset the heartbeats.
STALL_S = 60.0


def make_planner(campaign: Campaign, drive_id: str, cycle: int, workload: WorkloadSpec,
                 budget_cap: int | None = None, key: str | None = None) -> Planner:
    """Planner for :func:`run_cycle`: sizes the file set from the free space.

    ``key`` replaces the drive ID in the content keys (recovery checks pass a
    fresh key so that a drive can never pass by returning an older check's data).
    """
    def planner(volume: VolumeUsage):
        budget = fill_budget(volume.free, campaign.cycle.fill_fraction,
                             campaign.cycle.reserve_bytes)
        if budget_cap is not None:
            budget = min(budget, budget_cap)
        return plan_cycle(seed=campaign.seed, drive_id=key or drive_id, cycle=cycle,
                          workload=workload, budget_bytes=budget,
                          cluster_size=max(volume.cluster_size, MIN_PLANNING_CLUSTER),
                          chunk_size=campaign.cycle.chunk_size)
    return planner


def check_drive(mount: Path, campaign: Campaign, drive_id: str, *, host: str = "",
                port: str = "", stop: threading.Event | None = None,
                progress: Callable[[], None] | None = None) -> CycleResult:
    """Short write-verify-delete check with fresh data (not counted as a test cycle)."""
    key = f"{drive_id}:check:{uuid.uuid4().hex}"
    planner = make_planner(campaign, drive_id, 0, CHECK_WORKLOAD, budget_cap=CHECK_BUDGET,
                           key=key)
    return run_cycle(mount, planner, campaign.cycle, drive_id=drive_id, cycle=0,
                     workload=CHECK_WORKLOAD.name, host=host, port=port, stop=stop,
                     progress=progress)


def check_summary(result: CycleResult) -> dict:
    return {"outcome": result.outcome.value, "write_mbps": round(result.write.mbps, 3),
            "verify_mbps": round(result.verify.mbps, 3), "bad_files": len(result.bad_files),
            "errors": [e.detail for e in result.errors if not e.recovered][:5]}


def log_failed_cycle(log: DriveLog, campaign: Campaign, state: DriveState, *, host: str,
                     port: str, outcome: Outcome, reason: str) -> None:
    """Record a cycle that could not run (drive missing, worker hung) as a failed cycle.

    It gets a cycle row and a cycle-failure event like any other failure, so it
    counts in the life table, the per-cycle model and the intermittent limit.
    """
    cycle = state.next_cycle
    now = datetime.now(timezone.utc)
    result = CycleResult(drive_id=state.drive_id, cycle=cycle,
                         workload=campaign.workload_for_cycle(cycle).name, host=host, port=port,
                         started_at=now, ended_at=now, outcome=outcome, cache_bypassed=False)
    result.errors.append(ErrorRecord("setup", ErrorKind.DISCONNECTED, "", reason, 1, False))
    log.append_cycle(result)
    for event in apply_cycle(state, result, campaign.failure):
        log.append_event(event)
    log.save_state(state)


def conclude_failure(log: DriveLog, state: DriveState, campaign: Campaign, code: str,
                     reason: str, **detail) -> DriveStatus:
    """After a failure the automatic protocol cannot clear: hard failure or attention."""
    policy = campaign.failure
    state.soft_failures = log.failure_cycles()
    if intermittent_limit_reached(state, policy):
        log.append_event(Event(state.drive_id, state.cycles_done, EventType.HARD_FAILURE, code, {
            "reason": f"{policy.intermittent_limit} cycle failures within "
                      f"{policy.intermittent_window} cycles",
            "soft_failure_cycles": state.soft_failures[-policy.intermittent_limit:]}))
        state.status = DriveStatus.FAILED
    else:
        log.append_event(Event(state.drive_id, state.cycles_done, EventType.ATTENTION, code, {
            "reason": reason,
            "next_steps": "re-plug into the spare port and run `flashrel recheck`; "
                          "retire with `flashrel retire` if it fails again", **detail}))
        state.status = DriveStatus.ATTENTION
    log.save_state(state)
    return state.status


@dataclass(frozen=True)
class Progress:
    """What a worker reports to its supervisor after each cycle."""

    drive_id: str
    status: str
    cycle: int
    workload: str
    outcome: str
    write_mbps: float
    verify_mbps: float
    soft_failures: int
    message: str = ""


class DriveWorker:
    """Cycles one drive until it fails, needs an operator, or is censored."""

    def __init__(self, campaign: Campaign, drive_id: str, host: str, stop: threading.Event,
                 report: Callable[[Progress], None] | None = None, heartbeat=None) -> None:
        self.campaign = campaign
        self.drive_id = drive_id
        self.host = host
        self.port = campaign.assignments[drive_id].port
        self.stop = stop
        self.report = report or (lambda progress: None)
        self.log = DriveLog(campaign.run_dir, drive_id)
        self.beat = None
        if heartbeat is not None:
            def beat() -> None:
                heartbeat.value = time.monotonic()
            self.beat = beat

    def run(self) -> DriveStatus:
        with drive_lock(self.campaign.name, self.drive_id):
            state = self.log.reconcile(self.log.load_state())
            if state.host and state.host != self.host:
                self._say(state, f"skipped: the drive was last tested on host {state.host}")
                return state.status
            if state.port:  # an operator may have moved the drive (recheck / note --port)
                self.port = state.port
            self.log.save_state(state)
            while not self.stop.is_set() and state.status is DriveStatus.ACTIVE:
                try:
                    self._one_cycle(state)
                except Exception as exc:  # log and hand over instead of crash-looping
                    self._needs_operator(state, f"unexpected error: {exc!r}",
                                         trace=traceback.format_exc(limit=4))
            return state.status

    # -- one iteration --------------------------------------------------------------------
    def _one_cycle(self, state: DriveState) -> None:
        policy = self.campaign.failure
        if self.beat:
            self.beat()
        if self.campaign.stop.reached(state.cycles_done, datetime.now()):
            self._event(state, EventType.CENSORED, "", {"cycles_done": state.cycles_done})
            self._set_status(state, DriveStatus.CENSORED)
            return
        mount = wait_for_drive(self.drive_id, policy.reconnect_timeout_s, stop=self.stop)
        if self.stop.is_set():
            return
        if mount is None:
            log_failed_cycle(self.log, self.campaign, state, host=self.host, port=self.port,
                             outcome=Outcome.DISCONNECTED,
                             reason="drive not found at the start of the cycle")
            conclude_failure(self.log, state, self.campaign, "F3",
                             "drive not found at the start of a cycle", port=self.port,
                             host=self.host)
            self._report_state(state)
            return
        cycle = state.next_cycle
        workload = self.campaign.workload_for_cycle(cycle)
        result = run_cycle(mount, make_planner(self.campaign, self.drive_id, cycle, workload),
                           self.campaign.cycle, drive_id=self.drive_id, cycle=cycle,
                           workload=workload.name, host=self.host, port=self.port,
                           stop=self.stop, progress=self.beat)
        if result.outcome is Outcome.ABORTED:
            return
        self.log.append_cycle(result)
        for event in apply_cycle(state, result, policy):
            self.log.append_event(event)
        self.log.save_state(state)  # persisted before any recovery work
        if result.outcome.is_failure:
            self._after_failure(state, result)
        self.report(Progress(self.drive_id, state.status.value, cycle, workload.name,
                             result.outcome.value, round(result.write.mbps, 2),
                             round(result.verify.mbps, 2), len(state.soft_failures)))

    def _after_failure(self, state: DriveState, result: CycleResult) -> None:
        policy = self.campaign.failure
        code = FAILURE_CODES[result.outcome]
        state.soft_failures = self.log.failure_cycles()
        if intermittent_limit_reached(state, policy):
            conclude_failure(self.log, state, self.campaign, code, "intermittent limit")
            return
        mount = wait_for_drive(self.drive_id, policy.reconnect_timeout_s, stop=self.stop)
        if self.stop.is_set():
            return  # stopped mid-recovery: the failure is logged, the drive stays active
        if mount is None:
            self._needs_operator(state, "drive did not re-enumerate after the failure", code)
            return
        try:
            read_only = is_read_only(mount)
        except OSError:
            read_only = True
        if read_only:
            self._needs_operator(state, "volume is read-only after the failure", code)
            return
        check = check_drive(mount, self.campaign, self.drive_id, host=self.host,
                            port=self.port, stop=self.stop, progress=self.beat)
        if check.outcome is Outcome.ABORTED:
            return
        if check.outcome is Outcome.PASS:
            self._event(state, EventType.RECOVERED, "D2", {
                "failure_cycle": result.cycle, "recovered_by": "automatic re-check",
                "check": check_summary(check)})
            self.log.save_state(state)
        else:
            self._needs_operator(state, "recovery check failed", code,
                                 check=check_summary(check))

    # -- helpers --------------------------------------------------------------------------
    def _needs_operator(self, state: DriveState, reason: str, code: str = "",
                        **detail) -> None:
        self._event(state, EventType.ATTENTION, code, {
            "reason": reason, "port": self.port, "host": self.host,
            "next_steps": "re-plug into the spare port and run `flashrel recheck`; "
                          "retire with `flashrel retire` if it fails again", **detail})
        self._set_status(state, DriveStatus.ATTENTION)

    def _event(self, state: DriveState, type_: EventType, code: str, detail: dict) -> None:
        self.log.append_event(Event(self.drive_id, state.cycles_done, type_, code, detail))

    def _set_status(self, state: DriveState, status: DriveStatus) -> None:
        state.status = status
        self.log.save_state(state)
        self._report_state(state)

    def _report_state(self, state: DriveState, message: str = "") -> None:
        self.report(Progress(self.drive_id, state.status.value, state.cycles_done, "", "", 0.0,
                             0.0, len(state.soft_failures), message))

    def _say(self, state: DriveState, message: str) -> None:
        self._report_state(state, message)


def _worker_main(config_path: str, drive_id: str, host: str, stop, updates, heartbeat) -> None:
    """Entry point of a worker process.

    Ctrl+C is ignored here: only the supervisor reacts to it and sets ``stop``,
    so a worker always ends between files and never in the middle of logging.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    campaign = load_campaign(config_path)
    try:
        DriveWorker(campaign, drive_id, host, stop, report=updates.put,
                    heartbeat=heartbeat).run()
    except Exception:
        updates.put(Progress(drive_id, "crashed", -1, "", "", 0.0, 0.0, 0,
                             traceback.format_exc(limit=5)))
        raise


def record_hang(campaign: Campaign, drive_id: str, host: str, idle_s: float) -> DriveStatus:
    """Log a hung drive (its worker is already stopped) and hand it to the operator."""
    log = DriveLog(campaign.run_dir, drive_id)
    state = log.reconcile(log.load_state())
    port = state.port or campaign.assignments[drive_id].port
    reason = f"no I/O progress for {idle_s / 60:.0f} min"
    log_failed_cycle(log, campaign, state, host=host, port=port, outcome=Outcome.HANG,
                     reason=reason)
    return conclude_failure(log, state, campaign, "F3", f"{reason}; worker stopped",
                            port=port, host=host)


class Supervisor:
    """Starts, watches and restarts the drive workers of one host."""

    def __init__(self, config_path: str | Path, host: str, drive_ids: list[str] | None = None,
                 *, temperature: TemperatureSource | None = None,
                 temperature_period_s: float = 300.0, status_period_s: float = 60.0,
                 max_restarts: int = 5, stagger_s: float = 30.0,
                 echo: Callable[[str], None] = print) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.campaign = load_campaign(self.config_path)
        self.host = host
        assigned = [a.drive_id for a in self.campaign.drives_on(host)]
        self.drive_ids = drive_ids or assigned
        unknown = sorted(set(self.drive_ids) - set(self.campaign.assignments))
        if unknown:
            raise ValueError(f"drives not in the campaign: {unknown}")
        self.temperature = temperature or NullTemperatureSource()
        self.temperature_period_s = temperature_period_s
        self.status_period_s = status_period_s
        self.max_restarts = max_restarts
        self.stagger_s = stagger_s
        self.echo = echo
        self.latest: dict[str, Progress] = {}

    def run(self) -> None:
        ctx = mp.get_context("spawn")
        updates = ctx.Queue()
        stops = {d: ctx.Event() for d in self.drive_ids}
        beats = {d: ctx.Value("d", time.monotonic()) for d in self.drive_ids}
        procs: dict[str, mp.process.BaseProcess] = {}
        restarts = dict.fromkeys(self.drive_ids, 0)
        hung: set[str] = set()
        hang_timeout = self.campaign.failure.hang_timeout_s

        def start(drive_id: str) -> None:
            beats[drive_id].value = time.monotonic()
            proc = ctx.Process(target=_worker_main, name=f"flashrel-{drive_id}",
                               args=(self.config_path, drive_id, self.host, stops[drive_id],
                                     updates, beats[drive_id]))
            proc.start()
            procs[drive_id] = proc

        for i, drive_id in enumerate(self.drive_ids):
            if i:  # staggered starts keep drives on one dock out of phase
                time.sleep(self.stagger_s)
            start(drive_id)
        self.echo(f"[{self.host}] testing {', '.join(self.drive_ids)} "
                  f"(campaign {self.campaign.name}); Ctrl+C stops after the current file")
        next_temp = next_status = 0.0
        last_loop = time.monotonic()
        with keep_awake():
            try:
                while any(p.is_alive() for p in procs.values()):
                    self._drain(updates)
                    now = time.monotonic()
                    if now - last_loop > STALL_S:  # the host slept: nobody was idle
                        for beat in beats.values():
                            beat.value = now
                        self.echo(f"[{self.host}] resumed after {now - last_loop:.0f} s; "
                                  "watchdog reset")
                    last_loop = now
                    if now >= next_temp:
                        readings = self.temperature.read()
                        if readings:
                            append_temperature(self.campaign.run_dir, self.host, readings)
                        next_temp = now + self.temperature_period_s
                    if now >= next_status:
                        self.echo(self.status_table())
                        next_status = now + self.status_period_s
                    for drive_id, proc in list(procs.items()):
                        idle = time.monotonic() - beats[drive_id].value
                        if proc.is_alive() and drive_id not in hung and idle > hang_timeout:
                            hung.add(drive_id)
                            self.echo(f"[{self.host}] {drive_id}: no progress for "
                                      f"{idle / 60:.0f} min, stopping its worker")
                            stops[drive_id].set()
                            proc.terminate()
                            proc.join(timeout=30)
                            record_hang(self.campaign, drive_id, self.host, idle)
                            continue
                        crashed = not proc.is_alive() and proc.exitcode not in (0, None)
                        if crashed and drive_id not in hung \
                                and restarts[drive_id] < self.max_restarts:
                            restarts[drive_id] += 1
                            self.echo(f"[{self.host}] restarting {drive_id} "
                                      f"(exit {proc.exitcode}, restart {restarts[drive_id]})")
                            time.sleep(min(60, 5 * restarts[drive_id]))
                            start(drive_id)
                    time.sleep(1.0)
            except KeyboardInterrupt:
                self.echo(f"[{self.host}] stopping workers after the current file ...")
                for stop in stops.values():
                    stop.set()
                for proc in procs.values():
                    proc.join(timeout=600)
            finally:
                self._drain(updates)
                self.temperature.close()
        self.echo(self.status_table())

    def _drain(self, updates) -> None:
        while True:
            try:
                progress = updates.get_nowait()
            except queue.Empty:
                return
            self.latest[progress.drive_id] = progress
            if progress.message:
                self.echo(f"[{self.host}] {progress.drive_id}: {progress.message}")

    def status_table(self) -> str:
        header = (f"{'drive':8} {'status':10} {'cycle':>6} {'workload':8} {'outcome':12} "
                  f"{'write MB/s':>10} {'read MB/s':>9} {'soft':>4}")
        lines = [header, "-" * len(header)]
        for drive_id in self.drive_ids:
            p = self.latest.get(drive_id)
            if p is None:
                lines.append(f"{drive_id:8} {'starting':10}")
                continue
            lines.append(f"{drive_id:8} {p.status:10} {p.cycle:>6} {p.workload:8} {p.outcome:12} "
                         f"{p.write_mbps:>10.2f} {p.verify_mbps:>9.2f} {p.soft_failures:>4}")
        return "\n".join(lines)
