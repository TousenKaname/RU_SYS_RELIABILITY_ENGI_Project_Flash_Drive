"""One worker process per drive: cycles until the drive fails, needs the operator, or is censored.

The logs are the record of truth. The worker saves its state right after logging a
cycle; on start it brings a state that missed the last cycle row up to that row, finds a
cycle that was in progress when a previous worker or the host died, logs it as
interrupted (not counted) together with the wear it caused, and continues.

Recovery protocol after a cycle failure (automatic; the drive is never repaired):

1. wait up to 2 min for the drive to be present again;
2. restart the USB device (``pnputil /restart-device``, the software equivalent of
   re-plugging it) and wait for it to come back;
3. no readable file system? -> attention; read-only? capacity changed? -> end of life
   (F4 / F6);
4. check the data at rest and the file-system records (sentinel);
5. write, verify and delete 64 MiB of fresh data in ``FLASHREL-CHECK`` (the leftovers of
   the failed cycle are NOT removed first, unlike run 1);
6. passed: remove the failed cycle's folder (retried like any delete; a folder that still
   cannot be removed is quarantined and left alone from then on). Not passed: the folder
   stays on the drive as evidence for the post-test protocol.

A drive that passes step 5 keeps cycling and the failure is intermittent (D2); a drive
that does not stops in the *attention* state until the post-test protocol (re-check on
another port, port check with a spare). Three cycle failures within ten cycles end its
life.

Hub events: when a drive vanishes, its worker records the moment (``lost.json``) and,
after a short settle time, counts the drives of the same hub that vanished within
``hub_window_s`` of it (their own records and the controller's presence scans). Three or
more is a hub event (F5): not counted, and the drive waits for the hub. A drive that does
not return within the reconnect timeout after the hub's other drives are back has failed
on its own (F3).

Every wait loop of the main thread advances the ``beat`` in live.json, so the
controller's hang watchdog can watch every phase: a blocking call that does not return
for 10 min is a hang (F3) wherever it happens.
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
from datetime import datetime
from typing import Any

from flashrel import __version__
from flashrel.config import Campaign, Workload
from flashrel.cycle import CycleResult, CycleRunner, ErrorRecord, Hooks
from flashrel.failure import (CODES, FINAL, FROM_CODE, Dose, DriveState, Outcome, Status,
                              life_record, limit_reached, track_throughput)
from flashrel.payload import CHECK_DIR, PAYLOAD_DIR, fill_budget, plan_cycle
from flashrel.platform import Platform, Unsupported, Volume, get_platform
from flashrel.sentinel import COMPONENTS, SentinelChecker, SentinelResult, locate, read_identity
from flashrel.store import CampaignStore, DriveStore, FileLock, StoreError, atomic_write_json, read_json
from flashrel.timeutil import iso, parse_iso, utc_now
from flashrel.units import KiB, MiB, mb_per_s

CHECK_WORKLOAD = Workload("check", 64 * KiB, 8 * MiB, 64)
CONTROLLER_STALE_S = 180.0
PRESENCE_STALE_S = 90.0
LIVE_PERIOD_S = 2.0
TRACE_PERIOD_S = 60.0
IO_PHASES = {"setup", "write", "verify", "delete", "check", "sentinel", "cleanup"}
UNWATCHED_PHASES = {"starting", "stopped"}  # the hang watchdog watches every other phase


class _Stopped(Exception):
    """A stop request arrived during a step that is resumed or redone later."""


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
            if "phase" in values and values["phase"] != self.data.get("phase"):
                self.data["beat"] += 1  # a new phase is progress too
                self.data["phase_started"] = iso()
            self.data.update(values)
            self.dirty = True

    def beat(self) -> None:
        """The main thread is alive and not stuck: every wait loop calls this."""
        with self.lock:
            self.data["beat"] += 1
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
        self.stop_at: datetime | None = None        # both read from run.json in main()
        self.started_at: datetime = utc_now()
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
        self._lost_at: datetime | None = None  # when _find first missed the drive

    # -- entry point ------------------------------------------------------------------------
    def main(self) -> int:
        lock = FileLock(self.ds.lock_path)
        if not lock.acquire():
            print(f"{self.drive_id}: another worker holds the drive lock", flush=True)
            return 3
        watcher = threading.Thread(target=self._watch, daemon=True)
        try:
            try:
                run = self.cstore.run_state(strict=True)
                self.stop_at = parse_iso(run["stop_at"]) if run.get("stop_at") else None
                if run.get("started_at"):
                    self.started_at = parse_iso(run["started_at"])
                self.intake = read_json(self.ds.intake_json, {}, strict=True) or {}
                if not self.intake:
                    print(f"{self.drive_id}: not enrolled (no intake.json)", flush=True)
                    return 2
                self.state = self._load_state()
            except StoreError as exc:  # never start from a fresh state over a held one
                print(f"{self.drive_id}: {exc}; the controller restarts this worker later", flush=True)
                return 4
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
        raw = self.ds.load_state(strict=True)
        state = DriveState.from_json(raw) if raw else DriveState(self.drive_id)
        self._reconcile(state)
        if state.status == Status.ENROLLED.value:
            state.status = Status.ACTIVE.value
        if state.status in (Status.RECOVERING.value, Status.PAUSED.value) and not state.pending_recovery:
            state.status = Status.ACTIVE.value
        return state

    def _reconcile(self, state: DriveState) -> None:
        """The cycle rows win over a state file that missed the last one (the worker died
        between writing the row and saving the state): the cycle count, the wear and, for
        a failed cycle, its failure and the recovery protocol still to run."""
        rows = self.ds.read_cycles()
        if not rows:
            return
        last = max(rows, key=lambda r: int(r["cycle"]))
        n = int(last["cycle"])
        if n <= state.cycles_done:
            return
        state.cycles_done = n
        if state.in_progress and int(state.in_progress.get("cycle") or 0) <= n:
            state.in_progress = None  # that cycle ended: its row is written
        state.last_outcome = last.get("outcome") or state.last_outcome
        for column, name in (("cum_bytes_written", "bytes_written"), ("cum_bytes_read", "bytes_read"),
                             ("cum_files_created", "files_created"), ("cum_files_deleted", "files_deleted")):
            value = _int(last.get(column))
            if value > getattr(state.cum, name):
                setattr(state.cum, name, value)
        code, outcome = last.get("code") or "", last.get("outcome") or ""
        if code and code != "F5" and outcome not in ("pass", "aborted", "interrupted", "hub_fault") \
                and n not in state.failure_cycles:
            phase = _row_phase(last)
            state.failure_cycles.append(n)
            if state.first_failure is None:
                state.first_failure = life_record(n, code, last.get("ended_at") or iso(), state.cum,
                                                  _float(last.get("hours_on_test")), phase=phase,
                                                  outcome=outcome)
            if state.status not in FINAL:
                state.status = Status.RECOVERING.value
                state.pending_recovery = {"cycle": n, "code": code, "phase": phase, "detail": {}}
        self.ds.event("note", n, text=f"state brought up to cycle {n} from cycles.csv (the last worker "
                                      "stopped between writing the row and saving the state)")

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
            if now >= next_run:  # "End test now" moves the stop time forward: seen within 0.5 s
                next_run = now + 0.5
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
    def _mine_at(self, root: str) -> Volume | None:
        try:
            vol = self.platform.volume_at(root)
        except OSError:
            return None
        if vol is None or not vol.fs:
            return None
        ident = read_identity(vol)
        if ident and ident.get("drive_id") == self.drive_id and ident.get("token") == self.intake.get("token"):
            return vol
        return None

    def _locate_quick(self) -> tuple[Volume | None, str]:
        """The drive at its last volume, or where the controller's last scan saw it: a
        normal cycle start never touches the other sticks (one of which may be hanging)."""
        hint = (self._presence().get(self.drive_id) or {}).get("root") or ""
        for root in dict.fromkeys(r for r in (self._last_root, hint) if r):
            vol = self._mine_at(root)
            if vol is not None:
                return vol, "identity"
        return None, ""

    def _find(self, wait: float = 0.0) -> tuple[Volume | None, str]:
        """Find the drive (quick look first, then a scan of every volume: identity file,
        then USB serial), waiting up to ``wait`` s for it to come back. When it is missing
        at the first look, that moment goes to ``_lost_at`` and lost.json."""
        deadline = time.monotonic() + wait
        first = True
        while True:
            self.live.beat()
            vol, how = self._locate_quick()
            if vol is None:
                vol, how = self._scan()
            if vol is not None:
                self._last_root = vol.root
                self._lost_at = None
                return vol, how
            if first:
                first = False
                self._lost_at = utc_now()
                self._note_lost(self._lost_at)
            if time.monotonic() >= deadline or self.stop.is_set():
                return None, ""
            self._sleep(min(3.0, max(0.2, deadline - time.monotonic())))

    def _scan(self) -> tuple[Volume | None, str]:
        """A look at every volume. Marked in live.json: a scan that hangs may be held up by
        another stick, which the hang watchdog takes into account. A scan that fails (a
        volume vanishing under it) finds nothing this time."""
        self.live.update(scan=True)
        try:
            return locate(self.platform, self.drive_id, self.intake)
        except OSError:
            return None, ""
        finally:
            self.live.update(scan=False)

    def _presence(self) -> dict[str, dict[str, Any]]:
        """The controller's last presence scan, per drive ({} when older than 90 s)."""
        presence = read_json(self.cstore.presence_json, {}) or {}
        try:
            if (utc_now() - parse_iso(presence["time"])).total_seconds() > PRESENCE_STALE_S:
                return {}
        except (KeyError, ValueError, TypeError):
            return {}
        return presence.get("drives") or {}

    def _note_lost(self, when: datetime) -> None:
        """Tell the other workers of the hub when this drive vanished (one record per
        disappearance: a later look within the hub window keeps the first moment)."""
        prev = read_json(self.ds.lost_json, {}) or {}
        try:
            if 0 <= (when - parse_iso(prev["time"])).total_seconds() <= self.policy.hub_window_s:
                return
        except (KeyError, ValueError, TypeError):
            pass
        try:
            atomic_write_json(self.ds.lost_json, {"time": iso(when), "drive": self.drive_id})
        except OSError:
            pass

    def _hub_mates_lost(self, lost_at: datetime) -> list[str]:
        """The other drives of this hub that vanished within hub_window_s of ``lost_at``,
        by their workers' lost.json and by the controller's presence scans."""
        if self.slot is None:
            return []
        window = self.policy.hub_window_s
        presence = self._presence()

        def near(stamp: Any) -> bool:
            try:
                return abs((parse_iso(stamp) - lost_at).total_seconds()) <= window
            except (TypeError, ValueError):
                return False

        mates = []
        for s in self.c.slots:
            if s.hub != self.slot.hub or s.drive == self.drive_id:
                continue
            lost = read_json(self.cstore.dir / "drives" / s.drive / "lost.json", {}) or {}
            seen = presence.get(s.drive) or {}
            gap = seen.get("last_missing") or {}
            try:  # a gap the controller saw that overlaps the window around lost_at
                overlap = ((parse_iso(gap["from"]) - lost_at).total_seconds() <= window
                           and (lost_at - parse_iso(gap["to"])).total_seconds() <= window)
            except (KeyError, TypeError, ValueError):
                overlap = False
            # Presence evidence only for drives still on test: a retired stick that keeps
            # dropping in and out must not make every disconnect on its hub look like a hub event.
            if near(lost.get("time")) or (seen.get("expected") and (near(seen.get("missing_since")) or overlap)):
                mates.append(s.drive)
        return mates

    def _hub_event(self, lost_at: datetime | None) -> bool:
        """True when this drive and at least hub_event_drives - 1 others of its hub vanished
        within hub_window_s of each other: a hub or power event (F5, not counted). Waits up
        to hub_settle_s, so the other workers and the controller's scan see it too."""
        if self.slot is None or lost_at is None:
            return False
        need = self.policy.hub_event_drives - 1
        end = time.monotonic() + self.policy.hub_settle_s
        while True:
            self.live.beat()
            if len(self._hub_mates_lost(lost_at)) >= need:
                return True
            left = end - time.monotonic()
            if left <= 0 or self.stop.is_set():
                return False
            self._sleep(min(2.0, left))

    def _unmounted(self) -> Volume | None:
        """The drive's volume when it is on the bus without a readable file system."""
        vol, _how = self._scan()
        return vol if vol is not None and not vol.fs else None

    def _hub_back(self, mates: list[str]) -> bool:
        presence = self._presence()
        return any((presence.get(d) or {}).get("present") for d in mates)

    def _wait_hub(self, cycle: int, lost_at: datetime, where: str) -> str:
        """A hub event (F5, not counted): wait for the hub. Returns "back" when this drive
        is back; "lost" when the hub's other drives came back and this one did not within
        reconnect_timeout_s (a failure of its own); "stopped" on a stop request."""
        mates = self._hub_mates_lost(lost_at)
        self.ds.event("hub_event", cycle, "F5", where=where, lost_at=iso(lost_at),
                      drives=sorted(mates + [self.drive_id]),
                      text="several drives of this hub vanished at once; not counted against the drive")
        st = self.state
        before = st.status
        st.status = Status.PAUSED.value
        self._save()
        self.live.update(phase="paused (hub)", context="hub")
        period = max(0.5, min(5.0, self.policy.reconnect_timeout_s / 4))
        hub_back: float | None = None
        next_scan = 0.0
        try:
            while not self.stop.is_set():
                self.live.beat()
                vol, how = self._locate_quick()
                if vol is None and time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + 30.0
                    vol, how = self._scan()
                if vol is not None:
                    self._last_root, self._lost_at = vol.root, None
                    self.ds.event("note", cycle, text=f"drive back after the hub event (found by {how})")
                    return "back"
                if hub_back is None:
                    if self._hub_back(mates):
                        hub_back = time.monotonic()
                elif time.monotonic() - hub_back >= self.policy.reconnect_timeout_s:
                    self.ds.event("note", cycle, text="the hub's other drives are back, this one is not")
                    return "lost"
                self._sleep(period)
            return "stopped"
        finally:
            if st.status == Status.PAUSED.value:
                st.status = before if before != Status.PAUSED.value else Status.ACTIVE.value
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
        self.live.beat()
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
        self.live.update(status=self.state.status, phase="locating", context="start", cycle=cycle)
        vol, how = self._find(wait=self.policy.reconnect_timeout_s)
        if self.stop.is_set():
            return
        if vol is None:
            lost_at = self._lost_at or utc_now()
            reason = "drive not found at the start of the cycle"
            if self._hub_event(lost_at):
                if self._wait_hub(cycle, lost_at, "start of the cycle") != "lost":
                    return
                self._failed_without_run(cycle, Outcome.DISCONNECTED, "did not come back after the hub event, "
                                         "although the hub's other drives did", hub_settled=True)
                return
            self._failed_without_run(cycle, Outcome.DISCONNECTED, reason, lost_at=lost_at)
            return
        if not vol.fs:  # found by its USB serial, but Windows cannot read a file system on it
            self._failed_without_run(cycle, Outcome.FS_FAULT,
                                     "the volume has no readable file system (RAW or no media)")
            return
        self.live.update(mount=vol.display, letter=vol.letter, phase="sentinel")
        result = self._sentinel(vol, "cycle-start")
        if not result.ok:
            if result.code == "F3":  # vanished during the check: the next iteration looks for it
                self._sleep(2.0)
                return
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
        self.live.update(cycle=cycle, workload=workload.name, cycle_started=iso(), context="cycle")
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
        lost_at = None
        if result.outcome is Outcome.DISCONNECTED:
            lost_at = parse_iso(result.ended_at)
            self._note_lost(lost_at)
            if self._hub_event(lost_at):
                result.outcome = Outcome.HUB_FAULT
                self._log_interrupted(result, "hub event")
                if self._wait_hub(cycle, lost_at, f"{result.phase} phase of cycle {cycle}") == "lost":
                    self._failed_without_run(cycle, Outcome.DISCONNECTED, "did not come back after the hub "
                                             "event, although the hub's other drives did", phase=result.phase,
                                             hub_settled=True)
                return
            raw = self._unmounted()
            if raw is not None:  # still on the bus: its file system went, not the stick (F6)
                result.outcome = Outcome.FS_FAULT
                result.errors.append(ErrorRecord(result.phase, "unmounted", raw.display,
                                                 "the stick is still on the bus but its file system no "
                                                 "longer mounts (RAW or no media)", 1, False))
                lost_at = None
        self._record_cycle(result)
        if result.outcome.is_failure:
            self._recover(cycle, result.code, result.phase, self._failure_detail(result, lost_at=lost_at))

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

    def _failure_detail(self, r: CycleResult, lost_at: datetime | None = None,
                        hub_settled: bool = False) -> dict[str, Any]:
        return {
            "lost_at": iso(lost_at) if lost_at else None,
            "hub_settled": hub_settled,  # decided against the hub already: not looked at again
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
                            sentinel: dict[str, Any] | None = None, lost_at: datetime | None = None,
                            phase: str = "start", hub_settled: bool = False) -> None:
        """A cycle that could not start or finish (drive missing, damage found at its start,
        not back after a hub event) is a failed cycle."""
        now = iso()
        r = CycleResult(drive_id=self.drive_id, cycle=cycle,
                        workload=self.c.workload_for_cycle(cycle).name, started_at=now, ended_at=now,
                        outcome=outcome, phase=phase, sentinel=sentinel)
        r.errors.append(ErrorRecord(phase, outcome.value, "", reason, 1, False))
        self._record_cycle(r)
        self._recover(cycle, r.code, phase, self._failure_detail(r, lost_at=lost_at, hub_settled=hub_settled))

    # -- recovery protocol ------------------------------------------------------------------
    def _recover(self, cycle: int, code: str, phase: str, detail: dict[str, Any]) -> None:
        st = self.state
        st.status = Status.RECOVERING.value
        st.pending_recovery = {"cycle": cycle, "code": code, "phase": phase,
                               "detail": {"sentinel": detail.get("sentinel"), "lost_at": detail.get("lost_at"),
                                          "hub_settled": bool(detail.get("hub_settled"))}}
        self._save()
        self.live.update(phase="recovering", context="recovery")
        try:
            self._recover_steps(cycle, code, phase, detail)
        finally:
            if st.status != Status.RECOVERING.value and not self._recovery_interrupted:
                st.pending_recovery = None
                self._save()
            self._recovery_interrupted = False

    def _reattribute_to_hub(self, cycle: int) -> None:
        """The failure turned out to be part of a hub event (seen late): not counted (F5).
        Its row stays in cycles.csv; this event says that it does not count."""
        st = self.state
        if cycle in st.failure_cycles:
            st.failure_cycles.remove(cycle)
        if st.first_failure and int(st.first_failure.get("cycle", -1)) == cycle:
            st.first_failure = None
        self.ds.event("port_fault", cycle, "F5", reattributed_cycle=cycle,
                      text="other drives of the hub vanished at the same time: hub event, not counted")
        st.status = Status.ACTIVE.value
        st.pending_recovery = None
        self._save()

    def _recover_steps(self, cycle: int, code: str, phase: str, detail: dict[str, Any]) -> None:
        st, policy = self.state, self.policy
        steps: dict[str, Any] = {}
        self._record_damage(detail.get("sentinel"))
        vol, how = self._find(wait=policy.reconnect_timeout_s)
        if self.stop.is_set():
            return self._interrupted_recovery(cycle)
        if vol is None and not detail.get("hub_settled"):
            failed_at = _parse_time(detail.get("lost_at")) if code == "F3" else None
            when = failed_at or self._lost_at
            if self._hub_event(when):
                back = self._wait_hub(cycle, when, "recovery")  # type: ignore[arg-type]
                if back == "stopped":
                    return self._interrupted_recovery(cycle)
                if back == "back":
                    if failed_at is not None:  # the disconnect itself was the hub event
                        return self._reattribute_to_hub(cycle)
                    self.live.update(phase="recovering", context="recovery")
                    vol, how = self._find()  # a hub event during the protocol: the failure stands
        if limit_reached(st, policy):
            self._end_of_life(cycle, code, f"{policy.intermittent_limit} cycle failures within "
                              f"{policy.intermittent_window} cycles", steps)
            return
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
        after = " and a device restart" if "reset" in steps else ""
        if vol is None:
            return self._attention(cycle, code, "drive not present after the failure" + after, steps)
        if not vol.fs:
            steps["file_system"] = "unreadable (RAW or no media)"
            return self._attention(cycle, code, "no readable file system after the failure" + after, steps)
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
        steps["sentinel"] = {"ok": sres.ok, "failed": sres.failed, "failed_all": sres.failed_all,
                             "detail": sres.detail[:300]}
        if not sres.ok:
            self._record_damage(sres.to_json())
        check = self._fresh_check(vol)
        steps["check"] = _check_summary(check)
        if check.outcome is Outcome.ABORTED:
            return self._interrupted_recovery(cycle)
        if check.outcome is Outcome.PASS:
            steps["cleanup"] = self._clear_failed_cycle(vol, cycle)
            st.recoveries += 1
            st.status = Status.ACTIVE.value
            self.ds.event("recovered", cycle, "D2", failure_code=code, failure_phase=phase, **steps)
            self._save()
            return
        # The failed cycle's files stay on the drive: evidence for the post-test protocol.
        self._attention(cycle, code, "recovery check failed", steps)

    def _record_damage(self, sentinel: dict[str, Any] | None) -> None:
        """Damage once on record is not counted again: later checks look for new damage
        only. Every component of the finding goes on record, not just the first."""
        sentinel = sentinel or {}
        found = list(sentinel.get("failed_all") or ([sentinel["failed"]] if sentinel.get("failed") else []))
        new = [c for c in found if c in COMPONENTS and c not in self.state.sentinel_skip]
        if new:
            self.state.sentinel_skip.extend(new)
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
        """After a passed recovery check: remove the failed cycle's folder, so the drive goes
        on with its full space. Every removal is retried like in a cycle; a stop leaves the
        rest to the next cycle's setup; only a folder that still cannot be removed after the
        retries is quarantined (left alone from then on)."""
        rel = f"{PAYLOAD_DIR}/c{cycle:06d}"
        path = os.path.join(vol.root, PAYLOAD_DIR, f"c{cycle:06d}")
        if not os.path.isdir(path):
            return {"folder": rel, "result": "absent"}
        self.live.phase("cleanup")
        dose = Dose()
        outcome: dict[str, Any] = {"folder": rel, "result": "removed"}
        try:
            for base, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    self._remove_with_retries(self.platform.remove_file, os.path.join(base, name))
                    dose.files_deleted += 1
                    self.live.progress(1, 0)
                for name in dirs:
                    self._remove_with_retries(self.platform.remove_dir, os.path.join(base, name))
                    dose.dirs_deleted += 1
            self._remove_with_retries(self.platform.remove_dir, path)
            dose.dirs_deleted += 1
            outcome["files"] = dose.files_deleted
        except _Stopped:
            outcome = {"folder": rel, "result": "left to the next cycle's setup (stop request)",
                       "files_removed": dose.files_deleted}
        except OSError as exc:
            if not self.platform.present(vol.root):
                outcome = {"folder": rel, "result": "left to the next cycle's setup (the drive vanished)",
                           "error": str(exc)[:200], "files_removed": dose.files_deleted}
            else:
                self.state.quarantined.append(rel)
                outcome = {"folder": rel, "result": "quarantined", "error": str(exc)[:200],
                           "files_removed": dose.files_deleted}
                self.ds.event("note", cycle, text=f"{rel} cannot be removed and is left alone from now on",
                              error=str(exc)[:200])
        self.state.cum.add(dose)
        return outcome

    def _remove_with_retries(self, remove, path: str) -> None:
        retries = self.c.cycle.retries
        for attempt in range(1, retries + 2):
            if self.stop.is_set():
                raise _Stopped()
            try:
                remove(path)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt > retries:
                    raise
                self._sleep(self.c.cycle.retry_backoff_s * attempt)

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
        end = min(utc_now(), self.stop_at) if self.stop_at else utc_now()  # Type I: censored at the stop
        hours = round((end - self.started_at).total_seconds() / 3600.0, 4)
        self.ds.event("censored", st.cycles_done, cycles_done=st.cycles_done, dose=st.cum.to_json(),
                      hours_on_test=hours, fdw=round(self._fdw(), 4))
        self._save()


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    try:
        return parse_iso(value) if value else None
    except (TypeError, ValueError):
        return None


def _row_phase(row: dict[str, str]) -> str:
    """The phase of the first error recorded in a cycle row."""
    try:
        errors = json.loads(row.get("errors") or "[]")
        return str(errors[0].get("phase", "")) if errors else ""
    except (ValueError, AttributeError, IndexError, TypeError):
        return ""


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
