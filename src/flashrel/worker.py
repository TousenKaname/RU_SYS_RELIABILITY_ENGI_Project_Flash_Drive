"""One worker process per drive: cycles until the drive fails, needs the operator, or is censored.

The logs are the record of truth. The worker saves its state right after logging a
cycle; on start it finds a cycle that was in progress when a previous worker or the host
died, logs it as interrupted (not counted) together with the wear it caused, and
continues with the next cycle.

Recovery protocol after a cycle failure (automatic; the drive is never repaired):

1. wait up to 2 min for the drive to be present again;
2. restart the USB device (``pnputil /restart-device``, the software equivalent of
   re-plugging it) and wait for it to come back;
3. read-only? capacity changed? -> end of life (F4 / F6);
4. check the data at rest and the file-system records (sentinel);
5. write, verify and delete 64 MiB of fresh data in ``FLASHREL-CHECK`` (the leftovers of
   the failed cycle are NOT removed first, unlike run 1);
6. try to remove the failed cycle's folder; a folder that cannot be removed is
   quarantined and left alone from then on.

A drive that passes step 5 keeps cycling and the failure is intermittent (D2); a drive
that does not stops in the *attention* state until the post-test protocol (re-check on
another port, port check with a spare). Three cycle failures within ten cycles end its
life. When three or more drives of one hub vanish together, the event is a hub fault
(F5): it is not counted, and the drives wait for the hub to come back.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import uuid
from typing import Any

from flashrel import __version__
from flashrel.config import Campaign, Workload
from flashrel.cycle import CycleResult, CycleRunner, ErrorRecord, Hooks
from flashrel.failure import (CODES, FINAL, FROM_CODE, Dose, DriveState, Outcome, Status,
                              life_record, limit_reached, track_throughput)
from flashrel.payload import CHECK_DIR, PAYLOAD_DIR, fill_budget, plan_cycle
from flashrel.platform import Platform, Unsupported, Volume, get_platform
from flashrel.sentinel import SentinelChecker, SentinelResult, locate, read_identity
from flashrel.store import CampaignStore, DriveStore, FileLock, read_json
from flashrel.timeutil import iso, parse_iso, utc_now
from flashrel.units import KiB, MiB, mb_per_s

CHECK_WORKLOAD = Workload("check", 64 * KiB, 8 * MiB, 64)
CONTROLLER_STALE_S = 180.0
LIVE_PERIOD_S = 2.0
TRACE_PERIOD_S = 60.0
IO_PHASES = {"setup", "write", "verify", "delete", "check", "sentinel"}


class Live:
    """Current activity of the worker, written to live.json every 2 s for the dashboard."""

    def __init__(self, store: DriveStore, drive_id: str) -> None:
        self.store = store
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"drive": drive_id, "pid": os.getpid(), "status": "starting",
                                     "phase": "starting", "beat": 0, "software": __version__}
        self.dirty = True
        self._io_bytes = 0      # bytes moved since the worker started (across phases)
        self._io_files = 0
        self._samples: list[tuple[float, int, int]] = [(time.monotonic(), 0, 0)]

    def update(self, **values: Any) -> None:
        with self.lock:
            self.data.update(values)
            self.dirty = True

    def phase(self, name: str, **info: Any) -> None:
        with self.lock:
            self.data.update({"phase": name, "phase_started": iso(), "files_done": 0,
                              "bytes_done": 0, "files_total": info.get("files_total", 0),
                              "bytes_total": info.get("bytes_total", 0)})
            self.data["beat"] += 1
            self.dirty = True

    def progress(self, files: int, nbytes: int) -> None:
        with self.lock:
            d = self.data
            d["files_done"] = d.get("files_done", 0) + files
            d["bytes_done"] = d.get("bytes_done", 0) + nbytes
            d["beat"] += 1
            self._io_bytes += nbytes
            self._io_files += files
            self.dirty = True

    def _rates(self) -> None:
        """Throughput over the last ~10 s (rolling, so short phases still show a rate)."""
        now = time.monotonic()
        self._samples.append((now, self._io_bytes, self._io_files))
        while len(self._samples) > 2 and now - self._samples[1][0] >= 10.0:
            self._samples.pop(0)
        t0, b0, f0 = self._samples[0]
        span = now - t0
        if span >= 2.0:
            self.data["rate_mbps"] = round(mb_per_s(self._io_bytes - b0, span), 3)
            self.data["rate_fps"] = round((self._io_files - f0) / span, 2)

    def flush(self) -> None:
        with self.lock:
            self._rates()
            snapshot = dict(self.data)
            self.dirty = False
        snapshot["heartbeat"] = iso()
        try:
            self.store.save_live(snapshot)
        except OSError:
            pass


class Worker:
    def __init__(self, campaign: Campaign, cstore: CampaignStore, drive_id: str, platform: Platform,
                 *, sleep=time.sleep) -> None:
        self.c = campaign
        self.cstore = cstore
        self.drive_id = drive_id
        self.ds = cstore.drive(drive_id)
        self.platform = platform
        self.sleep_fn = sleep
        self.policy = campaign.failure
        self.slot = campaign.slot_of(drive_id)
        self.run = cstore.run_state()
        self.stop_at = parse_iso(self.run["stop_at"]) if self.run.get("stop_at") else None
        self.started_at = parse_iso(self.run["started_at"]) if self.run.get("started_at") else utc_now()
        self.stop = threading.Event()
        self.stop_reason = ""
        self.deadline_hit = False
        self.live = Live(self.ds, drive_id)
        self.intake: dict[str, Any] = {}
        self.state = DriveState(drive_id)
        self.checker: SentinelChecker | None = None
        self.next_sentinel = 0.0
        self.cycle_sentinel = {"checks": 0, "warnings": 0}
        self.current_dose: Dose | None = None
        self._last_trace = 0.0
        self._serial_noted = False
        self._recovery_interrupted = False
        self._last_root = ""

    # -- entry point ------------------------------------------------------------------------
    def main(self) -> int:
        lock = FileLock(self.ds.lock_path)
        if not lock.acquire():
            print(f"{self.drive_id}: another worker holds the drive lock", flush=True)
            return 3
        watcher = threading.Thread(target=self._watch, daemon=True)
        try:
            self.intake = self.ds.load_intake() or {}
            if not self.intake:
                print(f"{self.drive_id}: not enrolled (no intake.json)", flush=True)
                return 2
            self.state = self._load_state()
            self.checker = SentinelChecker(self.platform, self.intake, self.c.sentinel,
                                           sleep=self._sleep, cancelled=self.stop.is_set)
            watcher.start()
            self._resume_interrupted()
            while True:
                if self.state.status in FINAL:
                    break
                if self._deadline():
                    self._censor()
                    break
                if self.stop.is_set():
                    break
                try:
                    pending = self.state.pending_recovery
                    if pending:
                        self.ds.event("note", int(pending.get("cycle", 0)),
                                      text="resuming the recovery protocol of an earlier failure")
                        self._recover(int(pending["cycle"]), pending.get("code", ""),
                                      pending.get("phase", ""), pending.get("detail") or {})
                        continue
                    self._iteration()
                except Exception as exc:  # keep a crash from looping silently
                    self.ds.event("note", self.state.cycles_done, text=f"worker error: {exc!r}",
                                  trace=traceback.format_exc(limit=6))
                    self._save()
                    raise
        finally:
            self.live.update(status=self.state.status, phase="stopped" if self.state.status not in FINAL
                             else self.state.status, stop_reason=self.stop_reason)
            self.live.flush()
            lock.release()
        return 0

    def _load_state(self) -> DriveState:
        raw = self.ds.load_state()
        state = DriveState.from_json(raw) if raw else DriveState(self.drive_id)
        # The logs win over a state file that missed the last cycle (crash in between).
        rows = self.ds.read_cycles()
        last = max((int(r["cycle"]) for r in rows), default=0)
        if last > state.cycles_done:
            state.cycles_done = last
        if state.status == Status.ENROLLED.value:
            state.status = Status.ACTIVE.value
        if state.status in (Status.RECOVERING.value, Status.PAUSED.value) and not state.pending_recovery:
            state.status = Status.ACTIVE.value
        return state

    def _save(self) -> None:
        self.ds.save_state(self.state.to_json())
        self.live.update(status=self.state.status, cycles_done=self.state.cycles_done,
                         cum=self.state.cum.to_json(), failures=len(self.state.failure_cycles),
                         recoveries=self.state.recoveries, transients=self.state.transients,
                         first_failure=self.state.first_failure, end_of_life=self.state.end_of_life,
                         last_outcome=self.state.last_outcome, last_mbps=self.state.last_mbps,
                         fdw=round(self._fdw(), 3))

    # -- background: stop requests, controller liveness, deadline, live.json --------------
    def _watch(self) -> None:
        next_live = next_run = 0.0
        while True:
            now = time.monotonic()
            if now >= next_run:  # "End test now" moves the stop time forward
                next_run = now + 5.0
                stop_at = (self.cstore.run_state() or {}).get("stop_at")
                if stop_at:
                    try:
                        self.stop_at = parse_iso(stop_at)
                    except ValueError:
                        pass
            if self._deadline() and not self.stop.is_set():
                self.deadline_hit = True
                self.stop_reason = "stop time reached"
                self.stop.set()
            ctrl = self.ds.control()
            if ctrl.get("stop") and not self.stop.is_set():
                self.stop_reason = str(ctrl.get("reason") or "stop requested")
                self.stop.set()
            heartbeat = read_json(self.cstore.controller_json, {}) or {}
            try:
                age = (utc_now() - parse_iso(heartbeat["time"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = 0.0 if not heartbeat else CONTROLLER_STALE_S + 1
            if age > CONTROLLER_STALE_S and not self.stop.is_set():
                self.stop_reason = "controller stopped responding"
                self.stop.set()
            if now >= next_live:
                dose = self.current_dose
                if dose is not None:  # wear of the cycle in progress, should the worker die
                    self.live.update(cycle_dose=dose.to_json())
                self.live.flush()
                next_live = now + LIVE_PERIOD_S
            if now - self._last_trace >= TRACE_PERIOD_S and self.live.data.get("phase") in IO_PHASES:
                self._trace()
            time.sleep(0.5)

    def _trace(self) -> None:
        self._last_trace = time.monotonic()
        d = dict(self.live.data)
        try:
            self.ds.append_trace({"time": iso(), "drive": self.drive_id, "cycle": d.get("cycle"),
                                  "workload": d.get("workload"), "phase": d.get("phase"),
                                  "files_done": d.get("files_done"), "files_total": d.get("files_total"),
                                  "bytes_done": d.get("bytes_done"), "bytes_total": d.get("bytes_total"),
                                  "rate_mbps": d.get("rate_mbps"), "rate_fps": d.get("rate_fps")})
        except OSError:
            pass

    def _deadline(self) -> bool:
        return self.deadline_hit or (self.stop_at is not None and utc_now() >= self.stop_at)

    def _sleep(self, seconds: float) -> None:
        """Sleep, but wake at once when a stop is requested."""
        self.stop.wait(seconds)

    def _hours(self) -> float:
        return round((utc_now() - self.started_at).total_seconds() / 3600.0, 4)

    def _fdw(self) -> float:
        total = int(self.intake.get("usable_bytes") or 0)
        return self.state.cum.bytes_written / total if total else 0.0

    # -- locating the drive -----------------------------------------------------------------
    def _locate_fast(self) -> Volume | None:
        """Check the volume this drive had last time first: a normal cycle start then never
        touches the other sticks (one of which may be hanging)."""
        if not self._last_root:
            return None
        try:
            vol = self.platform.volume_at(self._last_root)
        except OSError:
            return None
        if vol is None or not vol.fs:
            return None
        ident = read_identity(vol)
        if ident and ident.get("drive_id") == self.drive_id and ident.get("token") == self.intake.get("token"):
            return vol
        return None

    def _find(self, wait: float = 0.0) -> tuple[Volume | None, str]:
        deadline = time.monotonic() + wait
        while True:
            fast = self._locate_fast()
            vol, how = (fast, "identity") if fast is not None else locate(self.platform, self.drive_id, self.intake)
            if vol is not None:
                self._last_root = vol.root
            if vol is not None or time.monotonic() >= deadline or self.stop.is_set():
                return vol, how
            if self._hub_event():
                return None, "hub"
            self._sleep(min(3.0, max(0.2, deadline - time.monotonic())))

    def _hub_event(self) -> bool:
        """True when the controller sees >= N drives of this drive's hub missing at once."""
        if self.slot is None:
            return False
        presence = read_json(self.cstore.presence_json, {}) or {}
        try:
            if (utc_now() - parse_iso(presence["time"])).total_seconds() > 90:
                return False  # the controller's scan is stuck: no basis for a hub event
        except (KeyError, ValueError, TypeError):
            return False
        drives = presence.get("drives", {})
        missing = [d for d, p in drives.items() if p.get("hub") == self.slot.hub and not p.get("present")
                   and p.get("expected")]
        return len(missing) >= self.policy.hub_event_drives

    def _wait_hub(self, cycle: int, detail: dict[str, Any]) -> None:
        """A hub event (F5, not counted): wait until the drive is back, then carry on."""
        self.ds.event("hub_event", cycle, "F5", **detail,
                      text="several drives of this hub vanished at once; not counted against the drive")
        self.state.status = Status.PAUSED.value
        self._save()
        self.live.update(phase="paused (hub)")
        while not self.stop.is_set():
            vol, how = locate(self.platform, self.drive_id, self.intake)
            if vol is not None:
                self.ds.event("note", cycle, text=f"drive back after the hub event (found by {how})")
                break
            self._sleep(5.0)
        self.state.status = Status.ACTIVE.value
        self._save()

    # -- sentinel ---------------------------------------------------------------------------
    def _sentinel(self, vol: Volume, phase: str) -> SentinelResult:
        assert self.checker is not None
        prev_phase = self.live.data.get("phase")
        result = self.checker.check(vol, skip=set(self.state.sentinel_skip))
        cum = self.state.cum.copy()
        if self.current_dose is not None:
            cum.add(self.current_dose)
        comps = result.components
        self.ds.append_sentinel({
            "time": result.time, "drive": self.drive_id, "cycle": self.live.data.get("cycle"),
            "phase": phase, "ok": result.ok, "failed": result.failed, "code": result.code,
            "reads": result.reads, "elapsed_s": result.elapsed_s,
            **{c: _comp_cell(comps.get(c)) for c in ("identity", "sentinel", "boot", "upcase", "rootdir")},
            "dirty": result.dirty, "warnings": result.warnings, "detail": result.detail,
            "cum_bytes_written": cum.bytes_written, "cum_files_created": cum.files_created,
            "cum_files_deleted": cum.files_deleted,
        })
        self.cycle_sentinel["checks"] += 1
        self.cycle_sentinel["warnings"] += len(result.warnings)
        if result.warnings:
            self.ds.event("sentinel_warning", self.state.cycles_done, warnings=result.warnings[:5])
        self.live.update(last_sentinel={"time": result.time, "ok": result.ok, "failed": result.failed,
                                        "detail": result.detail[:200]}, phase=prev_phase)
        self.next_sentinel = time.monotonic() + self.c.sentinel.interval_s
        return result

    def _periodic(self, vol: Volume, phase: str) -> None:
        boundary = phase.endswith("-end") or phase == "setup"
        if not boundary and time.monotonic() < self.next_sentinel:
            return
        if phase == "setup":
            return  # checked right before the cycle started
        result = self._sentinel(vol, phase)
        if not result.ok:
            from flashrel.cycle import CycleAborted

            outcome = FROM_CODE.get(result.code, Outcome.FS_FAULT)
            raise CycleAborted(outcome, ErrorRecord(phase, "sentinel", result.failed, result.detail, 1, False),
                               sentinel=result.to_json())

    # -- one iteration ----------------------------------------------------------------------
    def _iteration(self) -> None:
        cycle = self.state.cycles_done + 1
        self.live.update(status=self.state.status, phase="locating", cycle=cycle)
        vol, how = self._find(wait=self.policy.reconnect_timeout_s)
        if self.stop.is_set():
            return
        if how == "hub":
            self._wait_hub(cycle, {"where": "start of the cycle"})
            return
        if vol is None:
            self._failed_without_run(cycle, Outcome.DISCONNECTED, "drive not found at the start of the cycle")
            return
        self.live.update(mount=vol.display, letter=vol.letter, phase="sentinel")
        result = self._sentinel(vol, "cycle-start")
        if not result.ok:
            if result.code == "F3":
                return  # vanished during the check: the next iteration decides
            self._failed_without_run(cycle, FROM_CODE.get(result.code, Outcome.FS_FAULT),
                                     f"start-of-cycle check: {result.detail}", sentinel=result.to_json())
            return
        if how == "serial" and not self._serial_noted:
            self._serial_noted = True
            self.ds.event("note", cycle, text="identity file unreadable; drive located by USB serial")
        self._run_cycle(vol, cycle)

    def _planner(self, cycle: int, workload: Workload, *, key_space: str | None = None,
                 cap: int | None = None, root_dir: str = PAYLOAD_DIR):
        c = self.c

        def plan(free: int, cluster: int):
            budget = fill_budget(free, c.cycle.fill_fraction, c.cycle.reserve_bytes)
            if cap is not None:
                budget = min(budget, cap)
            return plan_cycle(seed=c.seed, drive_id=self.drive_id, cycle=cycle, workload=workload,
                              budget_bytes=budget, cluster_size=cluster, chunk_size=c.cycle.chunk_size,
                              key_space=key_space, root_dir=root_dir)
        return plan

    def _hooks(self, vol: Volume) -> Hooks:
        return Hooks(should_stop=self.stop.is_set,
                     progress=lambda phase, files, nbytes: self.live.progress(files, nbytes),
                     phase=lambda name, info: self.live.phase(name, **info),
                     periodic=lambda phase: self._periodic(vol, phase))

    def _run_cycle(self, vol: Volume, cycle: int) -> None:
        workload = self.c.workload_for_cycle(cycle)
        if self.state.started_at is None:
            self.state.started_at = iso()
            self.ds.event("started", 0, hub=self.slot.hub if self.slot else "",
                          port=self.slot.port if self.slot else 0, software=__version__)
        self.state.in_progress = {"cycle": cycle, "workload": workload.name, "started_at": iso()}
        self._save()
        self.cycle_sentinel = {"checks": 0, "warnings": 0}
        self.live.update(cycle=cycle, workload=workload.name, cycle_started=iso())
        runner = CycleRunner(self.platform, vol.root, self._planner(cycle, workload), self.c.cycle,
                             drive_id=self.drive_id, cycle=cycle, workload=workload.name,
                             hooks=self._hooks(vol), quarantined=set(self.state.quarantined),
                             sleep=self._sleep)
        self.current_dose = runner.result.dose
        result = runner.run()
        self.current_dose = None
        self.state.cum.add(result.dose)
        self.state.in_progress = None
        if result.outcome is Outcome.ABORTED:
            self._log_interrupted(result, self.stop_reason or "stopped")
            return
        if result.outcome is Outcome.DISCONNECTED and self._hub_event():
            result.outcome = Outcome.HUB_FAULT
            self._log_interrupted(result, "hub event")
            self._wait_hub(cycle, {"where": f"{result.phase} phase of cycle {cycle}"})
            return
        self._record_cycle(result)
        if result.outcome.is_failure:
            self._recover(cycle, result.code, result.phase, self._failure_detail(result))

    def _log_interrupted(self, result: CycleResult, reason: str) -> None:
        cut = self._deadline()
        self.ds.event("interrupted", result.cycle, reason=reason, at_stop_time=cut,
                      workload=result.workload, phase=result.phase, planned_bytes=result.planned_bytes,
                      planned_files=result.planned_files, free_before=result.free_before,
                      write_bytes=result.write.bytes, verify_bytes=result.verify.bytes,
                      files_written=result.write.files, files_verified=result.verify.files,
                      files_deleted=result.delete.files, files_corrupt=result.files_corrupt,
                      files_missing=result.files_missing, dose=result.dose.to_json(),
                      text=("stop time reached during the cycle; the cycle is not counted" if cut
                            else "cycle not completed; not counted"))
        self._save()

    def _record_cycle(self, r: CycleResult) -> None:
        """The cycle row is written first; the state follows it (the logs are the record)."""
        st = self.state
        group = self.c.group_of(self.drive_id).id
        self.ds.append_cycle({
            "campaign": self.c.name, "drive": self.drive_id, "group": group,
            "hub": self.slot.hub if self.slot else "", "port": self.slot.port if self.slot else "",
            "cycle": r.cycle, "workload": r.workload, "started_at": r.started_at, "ended_at": r.ended_at,
            "duration_s": r.duration_s, "outcome": r.outcome.value, "code": r.code,
            "planned_files": r.planned_files, "planned_bytes": r.planned_bytes,
            "free_before": r.free_before, "free_after": r.free_after,
            "write_files": r.write.files, "write_bytes": r.write.bytes, "write_s": r.write.seconds,
            "write_mbps": r.write.mbps, "write_p50_s": r.write.quantile(0.5),
            "write_p90_s": r.write.quantile(0.9), "write_p99_s": r.write.quantile(0.99),
            "write_max_s": max(r.write.file_seconds, default=0.0),
            "verify_files": r.verify.files, "verify_bytes": r.verify.bytes, "verify_s": r.verify.seconds,
            "verify_mbps": r.verify.mbps, "verify_p50_s": r.verify.quantile(0.5),
            "verify_p99_s": r.verify.quantile(0.99), "verify_max_s": max(r.verify.file_seconds, default=0.0),
            "delete_files": r.delete.files, "delete_s": r.delete.seconds, "delete_fps": r.delete.fps,
            "delete_p50_s": r.delete.quantile(0.5), "delete_p99_s": r.delete.quantile(0.99),
            "delete_max_s": max(r.delete.file_seconds, default=0.0),
            "files_missing": r.files_missing, "files_corrupt": r.files_corrupt,
            "files_unreadable": r.unreadable_files, "bytes_corrupt": r.bytes_corrupt,
            "bits_corrupt": r.bits_corrupt,
            "corruption_kinds": ";".join(f"{k}:{v}" for k, v in sorted(r.corruption_kinds.items())),
            "transient_errors": r.transient_errors,
            "errors": [e.__dict__ for e in r.errors][:10], "leftover_dirs": r.leftover_dirs,
            "space_leak": r.space_leak, "sentinel_checks": self.cycle_sentinel["checks"],
            "sentinel_warnings": self.cycle_sentinel["warnings"],
            "cum_bytes_written": st.cum.bytes_written, "cum_bytes_read": st.cum.bytes_read,
            "cum_files_created": st.cum.files_created, "cum_files_deleted": st.cum.files_deleted,
            "cum_fdw": round(self._fdw(), 4), "hours_on_test": self._hours(), "mount": r.mount,
            "cache_bypassed": r.cache_bypassed, "software": __version__,
        })
        st.cycles_done = r.cycle
        st.last_outcome = r.outcome.value
        if r.outcome is Outcome.PASS:
            st.last_mbps[r.workload] = {"write": round(r.write.mbps, 3), "read": round(r.verify.mbps, 3)}
        for name, stats in (("write", r.write), ("verify", r.verify), ("delete", r.delete)):
            if stats.files:
                self.ds.append_hist({"drive": self.drive_id, "cycle": r.cycle, "workload": r.workload,
                                     "phase": name, "files": stats.files, "seconds": round(stats.seconds, 3),
                                     "counts": stats.histogram()})
        if r.transient_errors:
            st.transients += r.transient_errors
            self.ds.event("transient", r.cycle, "D3", count=r.transient_errors,
                          mismatches_cleared=r.transient_mismatches,
                          errors=[e.__dict__ for e in r.errors if e.recovered][:10])
        if r.outcome is Outcome.PASS:
            slow = track_throughput(st, r.workload, r.write.mbps, self.policy)
            if slow:
                self.ds.event("slow", r.cycle, "D1", **slow)
        else:
            st.failure_cycles.append(r.cycle)
            if st.first_failure is None:
                st.first_failure = life_record(r.cycle, r.code, iso(), st.cum, self._hours(),
                                               phase=r.phase, outcome=r.outcome.value)
            self.ds.event("cycle_failure", r.cycle, r.code, **self._failure_detail(r),
                          dose=st.cum.to_json(), hours_on_test=self._hours(), fdw=round(self._fdw(), 4))
        self._save()

    def _failure_detail(self, r: CycleResult) -> dict[str, Any]:
        return {
            "outcome": r.outcome.value, "workload": r.workload, "phase": r.phase,
            "errors": [e.__dict__ for e in r.errors if not e.recovered][:20],
            "bad_files": [{"file": c.relpath, "status": c.status.value, "bytes": c.bytes_diff,
                           "bits": c.bits_diff, "kinds": sorted({d.kind.value for d in c.diffs})}
                          for c in r.bad_files[:20]],
            "files_corrupt": r.files_corrupt, "files_missing": r.files_missing,
            "files_unreadable": r.unreadable_files, "bits_corrupt": r.bits_corrupt,
            "sentinel": r.sentinel,
        }

    def _failed_without_run(self, cycle: int, outcome: Outcome, reason: str,
                            sentinel: dict[str, Any] | None = None) -> None:
        """A cycle that could not start (drive missing, damage found at its start) is a failed cycle."""
        now = iso()
        r = CycleResult(drive_id=self.drive_id, cycle=cycle,
                        workload=self.c.workload_for_cycle(cycle).name, started_at=now, ended_at=now,
                        outcome=outcome, phase="start", sentinel=sentinel)
        r.errors.append(ErrorRecord("start", outcome.value, "", reason, 1, False))
        self._record_cycle(r)
        self._recover(cycle, r.code, "start", self._failure_detail(r))

    # -- recovery protocol ------------------------------------------------------------------
    def _recover(self, cycle: int, code: str, phase: str, detail: dict[str, Any]) -> None:
        st = self.state
        st.status = Status.RECOVERING.value
        st.pending_recovery = {"cycle": cycle, "code": code, "phase": phase,
                               "detail": {"sentinel": detail.get("sentinel")}}
        self._save()
        try:
            self._recover_steps(cycle, code, phase, detail)
        finally:
            if st.status != Status.RECOVERING.value and not self._recovery_interrupted:
                st.pending_recovery = None
                self._save()
            self._recovery_interrupted = False

    def _hub_during_recovery(self, cycle: int) -> None:
        """The failure turned out to be part of a hub event: re-attribute it (F5, not counted)."""
        st = self.state
        if cycle in st.failure_cycles:
            st.failure_cycles.remove(cycle)
        if st.first_failure and int(st.first_failure.get("cycle", -1)) == cycle:
            st.first_failure = None
        self.ds.event("port_fault", cycle, "F5", reattributed_cycle=cycle,
                      text="other drives of the hub vanished at the same time: hub event, not counted")
        st.status = Status.ACTIVE.value
        st.pending_recovery = None
        self._wait_hub(cycle, {"where": "recovery"})

    def _recover_steps(self, cycle: int, code: str, phase: str, detail: dict[str, Any]) -> None:
        st, policy = self.state, self.policy
        self.live.update(phase="recovering")
        steps: dict[str, Any] = {}
        self._record_damage((detail.get("sentinel") or {}).get("failed"))
        if limit_reached(st, policy):
            self._end_of_life(cycle, code, f"{policy.intermittent_limit} cycle failures within "
                              f"{policy.intermittent_window} cycles", steps)
            return
        vol, how = self._find(wait=policy.reconnect_timeout_s)
        if self.stop.is_set():
            return self._interrupted_recovery(cycle)
        if how == "hub":
            return self._hub_during_recovery(cycle)
        steps["found_before_reset"] = how or "no"
        usb = self.intake.get("usb_instance")
        if policy.device_reset and self.platform.supports_reset and usb:
            try:
                steps["reset"] = self.platform.reset_device(usb)
            except Exception as exc:  # a failed restart is a recorded step, never a crash
                steps["reset"] = {"ok": False, "output": f"{type(exc).__name__}: {exc}"[:200]}
            self.ds.event("device_reset", cycle, **steps["reset"])
            self._sleep(5.0)
            vol, how = self._find(wait=policy.reconnect_timeout_s)
            if self.stop.is_set():
                return self._interrupted_recovery(cycle)
            steps["found_after_reset"] = how or "no"
        if vol is None:
            if how == "hub" or self._hub_event():
                return self._hub_during_recovery(cycle)
            return self._attention(cycle, code, "drive not present after the failure"
                                   + (" and a device restart" if "reset" in steps else ""), steps)
        try:
            if self.platform.is_read_only(vol.root):
                steps["read_only"] = True
                return self._end_of_life(cycle, "F4", "volume is read-only after the failure", steps)
        except OSError as exc:
            steps["read_only_error"] = str(exc)[:200]
        total = vol.total or 0
        usable = int(self.intake.get("usable_bytes") or 0)
        if total and usable and abs(total - usable) > 64 * MiB:
            steps["capacity"] = {"now": total, "intake": usable}
            return self._end_of_life(cycle, "F6", "volume capacity changed", steps)
        sres = self._sentinel(vol, "recovery")
        steps["sentinel"] = {"ok": sres.ok, "failed": sres.failed, "detail": sres.detail[:300]}
        if not sres.ok:
            self._record_damage(sres.failed)
        check = self._fresh_check(vol)
        steps["check"] = _check_summary(check)
        if check.outcome is Outcome.ABORTED:
            return self._interrupted_recovery(cycle)
        steps["cleanup"] = self._clear_failed_cycle(vol, cycle)
        if check.outcome is Outcome.PASS:
            st.recoveries += 1
            st.status = Status.ACTIVE.value
            self.ds.event("recovered", cycle, "D2", failure_code=code, failure_phase=phase, **steps)
            self._save()
            return
        self._attention(cycle, code, "recovery check failed", steps)

    def _record_damage(self, component: str | None) -> None:
        """Damage once on record is not counted again: later checks look for new damage only."""
        if component in ("identity", "sentinel", "boot", "upcase", "rootdir") \
                and component not in self.state.sentinel_skip:
            self.state.sentinel_skip.append(component)
            self._save()

    def _fresh_check(self, vol: Volume) -> CycleResult:
        key = f"{self.drive_id}:check:{uuid.uuid4().hex}"
        planner = self._planner(0, CHECK_WORKLOAD, key_space=key, cap=self.policy.check_bytes,
                                root_dir=CHECK_DIR)
        runner = CycleRunner(self.platform, vol.root, planner, self.c.cycle, drive_id=self.drive_id,
                             cycle=0, workload="check", hooks=Hooks(
                                 should_stop=self.stop.is_set,
                                 progress=lambda p, f, b: self.live.progress(f, b),
                                 phase=lambda name, info: self.live.phase("check", **info)),
                             data_dir=CHECK_DIR, clear_dirs=(CHECK_DIR,), sleep=self._sleep)
        result = runner.run()
        self.state.cum.add(result.dose)
        return result

    def _clear_failed_cycle(self, vol: Volume, cycle: int) -> dict[str, Any]:
        rel = f"{PAYLOAD_DIR}/c{cycle:06d}"
        path = os.path.join(vol.root, PAYLOAD_DIR, f"c{cycle:06d}")
        if not os.path.exists(path):
            return {"folder": rel, "result": "absent"}
        dose = Dose()
        try:
            for base, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    self.platform.remove_file(os.path.join(base, name))
                    dose.files_deleted += 1
                for name in dirs:
                    self.platform.remove_dir(os.path.join(base, name))
                    dose.dirs_deleted += 1
            self.platform.remove_dir(path)
            outcome = {"folder": rel, "result": "removed", "files": dose.files_deleted}
        except OSError as exc:
            self.state.quarantined.append(rel)
            outcome = {"folder": rel, "result": "quarantined", "error": str(exc)[:200],
                       "files_removed": dose.files_deleted}
            self.ds.event("note", cycle, text=f"{rel} cannot be removed and is left alone from now on",
                          error=str(exc)[:200])
        self.state.cum.add(dose)
        return outcome

    def _attention(self, cycle: int, code: str, reason: str, steps: dict[str, Any]) -> None:
        st = self.state
        st.status = Status.ATTENTION.value
        st.end_of_life = life_record(cycle, code, iso(), st.cum, self._hours(), reason=reason,
                                     confirmed=False)
        self.ds.event("attention", cycle, code, reason=reason, steps=steps,
                      next_steps="after the stop: re-check on another port, then port check with a spare")
        self._save()

    def _end_of_life(self, cycle: int, code: str, reason: str, steps: dict[str, Any]) -> None:
        st = self.state
        st.status = Status.FAILED.value
        st.end_of_life = life_record(cycle, code, iso(), st.cum, self._hours(), reason=reason,
                                     confirmed=True)
        self.ds.event("hard_failure", cycle, code, reason=reason, steps=steps)
        self._save()

    def _interrupted_recovery(self, cycle: int) -> None:
        self.ds.event("note", cycle, text=f"recovery protocol interrupted ({self.stop_reason}); "
                                          "it resumes with the next worker")
        self._recovery_interrupted = True
        self.state.status = Status.ACTIVE.value
        self._save()

    # -- resume and censoring ---------------------------------------------------------------
    def _resume_interrupted(self) -> None:
        st = self.state
        if not st.in_progress:
            return
        live = self.ds.load_live() or {}
        partial = Dose.from_json(live.get("cycle_dose"))
        st.cum.add(partial)
        self.ds.event("interrupted", int(st.in_progress.get("cycle", st.cycles_done + 1)),
                      reason="worker or host stopped during the cycle",
                      workload=st.in_progress.get("workload"), last_phase=live.get("phase"),
                      estimated_dose=partial.to_json(),
                      text="cycle not completed; not counted (wear estimated from the last live status)")
        st.in_progress = None
        self._save()

    def _censor(self) -> None:
        st = self.state
        if st.status in FINAL:
            return
        st.status = Status.CENSORED.value
        st.censored_at = iso()
        self.ds.event("censored", st.cycles_done, cycles_done=st.cycles_done, dose=st.cum.to_json(),
                      hours_on_test=self._hours(), fdw=round(self._fdw(), 4))
        self._save()


def _comp_cell(r: dict[str, Any] | None) -> str:
    if r is None:
        return ""
    if r.get("skipped"):
        return "on record"
    if r.get("ok"):
        return "ok"
    if r.get("bad_bytes"):
        return f"bad {r['bad_bytes']}B/{r.get('wrong_bits')}b"
    return "fail"


def _check_summary(r: CycleResult) -> dict[str, Any]:
    return {"outcome": r.outcome.value, "files": r.write.files, "bytes": r.write.bytes,
            "write_mbps": round(r.write.mbps, 3), "read_mbps": round(r.verify.mbps, 3),
            "bad_files": len(r.bad_files), "errors": [e.detail for e in r.errors if not e.recovered][:5]}


def worker_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flashrel worker")
    p.add_argument("--campaign-dir", required=True)
    p.add_argument("--drive", required=True)
    p.add_argument("--sim-root")
    p.add_argument("--sim-speed", type=float, default=1.0)
    args = p.parse_args(argv)
    from flashrel.config import Campaign

    campaign_dir = os.path.abspath(args.campaign_dir)
    with open(os.path.join(campaign_dir, "campaign.json"), encoding="utf-8") as fh:
        campaign = Campaign.from_json(json.load(fh))
    cstore = CampaignStore(os.path.dirname(campaign_dir), os.path.basename(campaign_dir))
    platform = get_platform(args.sim_root, args.sim_speed)
    print(f"{iso()} worker {args.drive} pid {os.getpid()} on {platform.name}", flush=True)
    code = Worker(campaign, cstore, args.drive, platform).main()
    print(f"{iso()} worker {args.drive} exit {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(worker_main())
