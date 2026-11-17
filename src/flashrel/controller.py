"""The controller: one per rig. Supervises the drive workers and serves the dashboard.

Main loop (once a second): heartbeat for the workers, presence of every drive, worker
start / restart / hang watchdog, the stop time, hub bandwidth samples, host sleep and
clock-jump detection, and the snapshot that the dashboard polls. Slow work (format,
self-test, post-test checks, USB inventory, export) runs in a background job thread, so
the loop never blocks; a worker treats a controller that is silent for 3 minutes as
gone and stops cleanly.

Campaign phases: setup -> running (-> pausing -> paused -> running) -> stopping ->
stopped (post-test protocol) -> finished.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from flashrel import __version__, analysis, export, hostops, intake, posttest
from flashrel.config import Campaign, for_simulation, load_campaign
from flashrel.cycle import ErrorRecord
from flashrel.failure import FINAL, DriveState, Status, life_record
from flashrel.platform import Platform
from flashrel.sentinel import locate, read_identity
from flashrel.store import (HUB_FIELDS, TEMPERATURE_FIELDS, CampaignStore, FileLock, append_csv,
                            atomic_write_json, read_json)
from flashrel.timeutil import iso, parse_iso, utc_now
from flashrel.worker import IO_PHASES

TICK_S = 1.0
PRESENCE_S = 5.0
SNAPSHOT_S = 1.5
SURVIVAL_S = 20.0
INVENTORY_S = 300.0
HUB_SAMPLE_S = 10.0
HUB_WRITE_S = 60.0
TEMPERATURE_S = 600.0
RESTART_DELAYS = (10, 30, 60, 120, 300)
GROUP_COLORS = {"A16": "#14b8a6", "A8": "#8b5cf6", "S8": "#f59e0b"}


class AlreadyRunning(RuntimeError):
    def __init__(self, url: str) -> None:
        super().__init__(f"the rig is already running at {url}")
        self.url = url


@dataclass
class Proc:
    drive: str
    popen: Any = None
    started: float = 0.0
    next_start: float = 0.0
    restarts: list[float] = field(default_factory=list)
    last_exit: int | None = None
    last_beat: int = -1
    beat_changed: float = 0.0
    hang: dict[str, Any] | None = None
    giving_up: bool = False
    adopted: int | None = None  # pid of a worker left running by an earlier controller


@dataclass
class Job:
    id: str
    kind: str
    args: dict[str, Any]
    status: str = "queued"
    progress: str = ""
    result: Any = None
    error: str = ""
    created: str = field(default_factory=iso)
    started: str = ""
    ended: str = ""

    def to_json(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "kind", "args", "status", "progress", "result",
                                               "error", "created", "started", "ended")}


class Controller:
    def __init__(self, *, config_path: str, data_dir: str, exports_dir: str, platform: Platform,
                 package_dir: str, sim_root: str | None = None, sim_speed: float = 1.0,
                 sim_hours: float = 0.5) -> None:
        self.platform = platform
        self.package_dir = package_dir
        self.data_dir = data_dir
        self.exports_dir = exports_dir
        self.sim_root = sim_root
        self.sim_speed = sim_speed
        base = load_campaign(config_path)
        self.campaign: Campaign = for_simulation(base, duration_h=sim_hours, speed=sim_speed) if sim_root else base
        self.cstore = CampaignStore(data_dir, self.campaign.name)
        self.lock = FileLock(self.cstore.lock_path)
        if not self.lock.acquire():
            info = read_json(self.cstore.controller_json, {}) or {}
            raise AlreadyRunning(info.get("url", "http://127.0.0.1:8585/"))
        frozen = read_json(self.cstore.campaign_json)
        self.run = self.cstore.run_state() or {"phase": "setup"}
        if frozen and self.run.get("phase") not in (None, "setup"):
            self.campaign = Campaign.from_json(frozen)  # a started test keeps its frozen design
        if sim_root:
            from flashrel.platform.sim import SimPlatform

            assert isinstance(platform, SimPlatform)
            platform.create_rig(self.campaign, seed=self.campaign.seed)
        self.token = secrets.token_urlsafe(18)
        self.url = ""
        self.procs: dict[str, Proc] = {d: Proc(d) for d in self.campaign.test_drives()}
        self.jobs: list[Job] = []
        self.job_queue: queue.Queue[Job] = queue.Queue()
        self.lock_state = threading.RLock()
        self.snapshot: dict[str, Any] = {}
        self.presence: dict[str, Any] = {"time": "", "drives": {}}
        self.inventory = read_json(self.cstore.host_dir / "inventory.json", {}) or {}
        self.checks: list[dict[str, Any]] = read_json(self.cstore.host_dir / "checks.json", []) or []
        self.events: dict[str, collections.deque] = {}
        self._event_offsets: dict[str, int] = {}
        self.host_events: collections.deque = collections.deque(maxlen=200)
        self.hub_series: dict[str, collections.deque] = {h: collections.deque(maxlen=720) for h in self.campaign.hubs}
        self._hub_acc: dict[str, list[dict[str, float]]] = {h: [] for h in self.campaign.hubs}
        self.survival: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.shutdown_requested = threading.Event()
        self._t = {k: 0.0 for k in ("presence", "snapshot", "survival", "inventory", "hub", "hubw",
                                     "temp", "awake")}
        self._last_wall, self._last_mono = time.time(), time.monotonic()
        self._started_mono = time.monotonic()
        self._resume_at = 0.0

    # ======================================================================================
    # lifecycle
    # ======================================================================================
    def start_background(self) -> None:
        self._started_wall = time.time()
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        threading.Thread(target=self._presence_loop, name="presence", daemon=True).start()
        threading.Thread(target=self._job_loop, name="jobs", daemon=True).start()
        threading.Thread(target=self._inventory_loop, name="inventory", daemon=True).start()
        self.cstore.host_event("controller_start", pid=os.getpid(), software=__version__,
                               platform=self.platform.name, phase=self.run.get("phase"),
                               admin=self.platform.is_admin(), sim=bool(self.sim_root))
        if self.run.get("phase") in ("running", "stopping"):
            self._resume_at = time.monotonic() + (3.0 if self.sim_root else 20.0)
            self.cstore.host_event("resume", text="controller restarted during the test; workers "
                                                  "restart in 20 s")
            self._schedule_starts(self._resume_at, 1.0 if self.sim_root else 5.0)
        if not read_json(self.cstore.host_dir / "host_info.json"):
            self.submit("host_info", {})
        self.submit("checks", {})

    def loop_forever(self) -> None:
        self.platform.keep_awake(True)
        try:
            while not self.shutdown_requested.is_set():
                t0 = time.monotonic()
                try:
                    self.tick()
                except Exception:
                    self._log(f"tick error:\n{traceback.format_exc()}")
                self.shutdown_requested.wait(max(0.05, TICK_S - (time.monotonic() - t0)))
        finally:
            self.platform.keep_awake(False)
            self._stop_all_workers("controller shutting down", wait=90.0)
            atomic_write_json(self.cstore.controller_json, {"time": iso(), "pid": os.getpid(),
                                                            "url": self.url, "stopped": True})
            self.cstore.host_event("controller_stop", phase=self.run.get("phase"))
            self.lock.release()

    def _log(self, text: str) -> None:
        line = f"{iso()} {text}"
        print(line, flush=True)

    # ======================================================================================
    # the main tick
    # ======================================================================================
    def _heartbeat_loop(self) -> None:
        """The workers' sign that the controller lives. A thread of its own, so a main loop
        held up by a stick that no longer answers never makes all 20 workers stop."""
        while not self.shutdown_requested.is_set():
            try:
                atomic_write_json(self.cstore.controller_json, {
                    "time": iso(), "pid": os.getpid(), "url": self.url, "phase": self.run.get("phase"),
                    "software": __version__, "loop_age_s": round(time.monotonic() - self._last_mono, 1)})
            except OSError as exc:
                self._log(f"heartbeat write failed: {exc}")
            self.shutdown_requested.wait(5.0)

    def _presence_loop(self) -> None:
        """Volume scans can block on a failing stick; they run here, off the main loop."""
        warned = False
        while not self.shutdown_requested.is_set():
            t0 = time.monotonic()
            self._update_presence()
            took = time.monotonic() - t0
            if took > 30 and not warned:
                warned = True
                self.cstore.host_event("slow_scan", seconds=round(took, 1),
                                       text="a volume scan took this long: a stick is not answering")
            self.shutdown_requested.wait(max(0.5, PRESENCE_S - took))

    def tick(self) -> None:
        now = time.monotonic()
        stop_file = Path(self.data_dir) / "STOP"
        if stop_file.exists():  # written by "Stop FlashRel.bat"
            stale = stop_file.stat().st_mtime < getattr(self, "_started_wall", 0) - 5
            try:
                stop_file.unlink()
            except OSError:
                pass
            if not stale:  # a STOP left from before this start is ignored
                self.cstore.host_event("stop_file", text="STOP file found: shutting down cleanly")
                self.shutdown_requested.set()
                return
        self._host_watch()
        if now - self._t["awake"] > 60:
            self.platform.keep_awake(True)
            self._t["awake"] = now
        phase = self.run.get("phase")
        if phase in ("running", "stopping", "pausing"):
            self._supervise(now)
        if now - self._t["hub"] >= HUB_SAMPLE_S:
            self._t["hub"] = now
            self._sample_hubs(now)
        if now - self._t["survival"] >= SURVIVAL_S:
            self._t["survival"] = now
            self.survival = self._survival()
        if now - self._t["snapshot"] >= SNAPSHOT_S:
            self._t["snapshot"] = now
            snap = self._build_snapshot()
            with self.lock_state:
                self.snapshot = snap

    def _host_watch(self) -> None:
        wall, mono = time.time(), time.monotonic()
        dwall, dmono = wall - self._last_wall, mono - self._last_mono
        if dmono > 30:
            self.cstore.host_event("stall", seconds=round(dmono, 1),
                                   text="the controller loop did not run for this long (host asleep or overloaded)")
            for proc in self.procs.values():  # nobody was idle on purpose: reset the watchdog
                proc.beat_changed = mono
        elif abs(dwall - dmono) > 20:
            self.cstore.host_event("clock_jump", seconds=round(dwall - dmono, 1),
                                   text="the wall clock jumped (time synchronisation or sleep)")
        self._last_wall, self._last_mono = wall, mono

    # ======================================================================================
    # presence
    # ======================================================================================
    def _update_presence(self) -> None:
        try:
            vols = self.platform.list_volumes()
        except Exception as exc:
            self._log(f"list_volumes failed: {exc}")
            return
        running = self.run.get("phase") in ("running", "stopping", "pausing")
        by_token = {}  # each volume's identity file is read once per scan
        for vol in vols:
            ident = read_identity(vol) if vol.fs else None
            if ident and ident.get("token"):
                by_token[(ident.get("drive_id"), ident.get("token"))] = vol
        drives = {}
        for unit in self.campaign.all_units():
            ds = self.cstore.drive(unit)
            intake_rec = ds.load_intake()
            if not intake_rec:
                continue
            vol = by_token.get((unit, intake_rec.get("token")))
            how = "identity" if vol is not None else ""
            if vol is None:
                vol, how = locate(self.platform, unit, intake_rec, volumes=[v for v in vols if not v.fs or v not in by_token.values()])
            state = ds.load_state() or {}
            slot = self.campaign.slot_of(unit)
            drives[unit] = {"present": vol is not None, "how": how, "root": vol.root if vol else "",
                            "letter": vol.letter if vol else "", "hub": slot.hub if slot else "",
                            "port": slot.port if slot else 0,
                            "expected": bool(running and slot and state.get("status") not in FINAL)}
        self.presence = {"time": iso(), "drives": drives}
        atomic_write_json(self.cstore.presence_json, self.presence)

    # ======================================================================================
    # workers
    # ======================================================================================
    def _schedule_starts(self, t0: float, stagger: float) -> None:
        for i, unit in enumerate(self.campaign.start_order()):
            proc = self.procs[unit]
            proc.next_start = t0 + i * stagger
            proc.giving_up = False

    def _worker_argv(self, unit: str) -> list[str]:
        argv = [self.platform.python_executable(), "-m", "flashrel", "worker",
                "--campaign-dir", str(self.cstore.dir), "--drive", unit]
        if self.sim_root:
            argv += ["--sim-root", self.sim_root, "--sim-speed", str(self.sim_speed)]
        return argv

    def _spawn(self, proc: Proc) -> None:
        ds = self.cstore.drive(proc.drive)
        ds.clear_control()
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        proc.popen = self.platform.spawn(self._worker_argv(proc.drive), str(ds.worker_log),
                                         cwd=self.package_dir, env=env)
        proc.started = time.monotonic()
        proc.beat_changed = proc.started
        proc.last_beat = -1
        self._log(f"started worker {proc.drive} (pid {proc.popen.pid})")

    def _supervise(self, now: float) -> None:
        phase = self.run.get("phase")
        if now < self._resume_at:
            return
        alive = 0
        for unit, proc in self.procs.items():
            state = self.cstore.drive(unit).load_state() or {}
            status = state.get("status")
            running = proc.popen is not None and proc.popen.poll() is None
            if running:
                alive += 1
                self._watchdog(proc, now)
                continue
            if proc.popen is None and proc.adopted:
                if self._adopted_alive(proc):
                    alive += 1
                    self._watchdog(proc, now)
                    continue
                self._log(f"adopted worker {unit} (pid {proc.adopted}) has ended")
                proc.adopted = None
                proc.next_start = now + 5
            if proc.popen is not None:  # just exited
                proc.last_exit = proc.popen.returncode
                proc.popen = None
                self._log(f"worker {unit} exited with {proc.last_exit} (status {status})")
                if proc.last_exit == 3:
                    # Lock busy: a worker of an earlier controller still drives this stick.
                    live = self.cstore.drive(unit).load_live() or {}
                    if live.get("pid"):
                        proc.adopted = int(live["pid"])
                        proc.beat_changed = now
                        self.cstore.host_event("worker_adopted", drive=unit, pid=proc.adopted)
                    proc.next_start = now + 60
                elif proc.last_exit not in (0, None):
                    proc.restarts = [t for t in proc.restarts if now - t < 3600] + [now]
                    delay = RESTART_DELAYS[min(len(proc.restarts) - 1, len(RESTART_DELAYS) - 1)]
                    proc.next_start = now + delay
                    self.cstore.host_event("worker_exit", drive=unit, exit=proc.last_exit,
                                           restart_in_s=delay, restarts_last_hour=len(proc.restarts))
                    if len(proc.restarts) > 6:
                        proc.giving_up = True
                        proc.next_start = now + 1800
                elif proc.last_exit == 0 and status not in FINAL:
                    proc.next_start = now + 10
            if phase != "running" or status in FINAL or not state:
                continue
            if proc.hang and not proc.hang.get("recorded"):
                self._record_hang(proc)
                continue
            if now >= proc.next_start and proc.next_start > 0:
                self._spawn(proc)
                alive += 1
        if phase == "running" and self.run.get("stop_at") and utc_now() >= parse_iso(self.run["stop_at"]):
            self._set_phase("stopping", reason="stop time reached")
        if phase in ("stopping", "pausing") and alive == 0:
            if phase == "pausing":
                self._set_phase("paused")
            else:
                self._censor_stragglers()
                self._set_phase("stopped")
                self.submit("summary", {})

    def _censor_stragglers(self) -> None:
        """Drives whose worker was not running at the stop are censored here (Type I)."""
        started = parse_iso(self.run["started_at"]) if self.run.get("started_at") else utc_now()
        for unit in self.campaign.test_drives():
            ds = self.cstore.drive(unit)
            raw = ds.load_state()
            if not raw or raw.get("status") in FINAL:
                continue
            lock = FileLock(ds.lock_path)
            if not lock.acquire():
                continue
            try:
                state = DriveState.from_json(raw)
                state.status = Status.CENSORED.value
                state.censored_at = iso()
                state.pending_recovery = None
                hours = round((utc_now() - started).total_seconds() / 3600, 4)
                ds.event("censored", state.cycles_done, cycles_done=state.cycles_done,
                         dose=state.cum.to_json(), hours_on_test=hours, by="controller",
                         text="no worker was running at the stop; censored at the cycles completed")
                ds.save_state(state.to_json())
            finally:
                lock.release()

    def _adopted_alive(self, proc: Proc) -> bool:
        """An adopted worker is alive while it holds the drive lock and its live.json is fresh."""
        ds = self.cstore.drive(proc.drive)
        live = ds.load_live() or {}
        if int(live.get("pid") or 0) != proc.adopted or live.get("phase") in ("stopped",):
            return False
        try:
            age = (utc_now() - parse_iso(live["heartbeat"])).total_seconds()
        except (KeyError, ValueError):
            return False
        return age < 30 and FileLock(ds.lock_path).held_by_other()

    def _kill(self, proc: Proc) -> None:
        try:
            if proc.popen is not None:
                proc.popen.terminate()
            elif proc.adopted:
                import signal

                os.kill(proc.adopted, signal.SIGTERM)  # TerminateProcess on Windows
        except OSError:
            pass

    def _watchdog(self, proc: Proc, now: float) -> None:
        live = self.cstore.drive(proc.drive).load_live() or {}
        beat = int(live.get("beat", -1))
        if beat != proc.last_beat:
            proc.last_beat, proc.beat_changed = beat, now
            return
        if proc.hang is not None:
            if now - proc.hang["since"] > 60 and not proc.hang.get("terminated"):
                self._kill(proc)
                proc.hang["terminated"] = True
                self.cstore.host_event("worker_terminated", drive=proc.drive, reason="hang")
            return
        idle = now - proc.beat_changed
        if live.get("phase") in IO_PHASES and idle > self.campaign.failure.hang_timeout_s:
            proc.hang = {"since": now, "idle_s": round(idle), "phase": live.get("phase"),
                         "cycle": live.get("cycle")}
            self.cstore.drive(proc.drive).request(stop=True, reason="hang watchdog")
            self.cstore.host_event("hang", drive=proc.drive, idle_s=round(idle), phase=live.get("phase"))

    def _record_hang(self, proc: Proc) -> None:
        """F3 hang: the worker made no progress for 10 min and was stopped. Recorded as a cycle
        failure; the next worker runs the recovery protocol (a second hang ends in attention)."""
        ds = self.cstore.drive(proc.drive)
        lock = FileLock(ds.lock_path)
        if not lock.acquire():
            return  # the process is still dying
        try:
            raw = ds.load_state() or {"drive_id": proc.drive}
            state = DriveState.from_json(raw)
            cycle = int((state.in_progress or {}).get("cycle") or state.cycles_done + 1)
            reason = f"no I/O progress for {proc.hang['idle_s'] // 60} min in the {proc.hang['phase']} phase"
            again = state.pending_recovery is not None
            state.in_progress = None
            if cycle > state.cycles_done:
                state.cycles_done = cycle
            state.failure_cycles.append(cycle)
            state.last_outcome = "hang"
            if state.first_failure is None:
                state.first_failure = life_record(cycle, "F3", iso(), state.cum, None, phase=proc.hang["phase"],
                                                  outcome="hang")
            group = self.campaign.group_of(proc.drive).id
            slot = self.campaign.slot_of(proc.drive)
            ds.append_cycle({"campaign": self.campaign.name, "drive": proc.drive, "group": group,
                             "hub": slot.hub if slot else "", "port": slot.port if slot else "",
                             "cycle": cycle, "workload": self.campaign.workload_for_cycle(cycle).name,
                             "started_at": iso(), "ended_at": iso(), "outcome": "hang", "code": "F3",
                             "errors": [ErrorRecord(proc.hang["phase"], "hang", "", reason, 1, False).__dict__],
                             "software": __version__})
            ds.event("cycle_failure", cycle, "F3", outcome="hang", reason=reason, dose=state.cum.to_json())
            if again:
                state.status = Status.ATTENTION.value
                state.pending_recovery = None
                state.end_of_life = life_record(cycle, "F3", iso(), state.cum, None, reason="hung again "
                                                "during the recovery protocol", confirmed=False)
                ds.event("attention", cycle, "F3", reason="hung again during the recovery protocol")
            else:
                state.status = Status.RECOVERING.value
                state.pending_recovery = {"cycle": cycle, "code": "F3", "phase": "hang", "detail": {}}
            ds.save_state(state.to_json())
            proc.hang["recorded"] = True
            proc.hang = None
            proc.next_start = time.monotonic() + 15
        finally:
            lock.release()

    def _stop_all_workers(self, reason: str, wait: float = 0.0) -> None:
        for unit, proc in self.procs.items():
            if (proc.popen is not None and proc.popen.poll() is None) or proc.adopted:
                self.cstore.drive(unit).request(stop=True, reason=reason)
        deadline = time.monotonic() + wait
        while wait and time.monotonic() < deadline:
            if all((p.popen is None or p.popen.poll() is not None) and not (p.adopted and self._adopted_alive(p))
                   for p in self.procs.values()):
                break
            time.sleep(0.5)

    def _set_phase(self, phase: str, **detail: Any) -> None:
        with self.lock_state:
            self.run["phase"] = phase
            self.run.setdefault("history", []).append({"time": iso(), "phase": phase, **detail})
            self.cstore.save_run_state(self.run)
        self.cstore.host_event("phase", phase=phase, **detail)
        self._log(f"phase -> {phase} {detail}")

    # ======================================================================================
    # operator actions (called from the HTTP thread)
    # ======================================================================================
    def readiness(self) -> dict[str, Any]:
        problems, warnings = [], []
        enrolled = intake.enrolled_drives(self.campaign, self.cstore)
        missing = [d for d in self.campaign.test_drives() if d not in enrolled]
        if missing:
            problems.append(f"not enrolled yet: {', '.join(missing)}")
        absent = [d for d in self.campaign.test_drives()
                  if d in enrolled and not self.presence.get("drives", {}).get(d, {}).get("present")]
        if absent:
            problems.append(f"not plugged in: {', '.join(absent)}")
        if any(j.status in ("queued", "running") and j.kind == "enroll" for j in self.jobs):
            problems.append("an enrollment is still running")
        keys: dict[str, set[str]] = {}
        policies = set()
        for d in self.campaign.test_drives():
            rec = enrolled.get(d) or {}
            if rec.get("hub_key"):
                keys.setdefault(rec["hub"], set()).add(rec["hub_key"])
            policies.add(rec.get("removal_policy"))
            if rec and not (rec.get("exfat") or {}).get("ok"):
                warnings.append(f"{d}: raw file-system checks are off")
        for hub, k in keys.items():
            if len(k) > 1:
                warnings.append(f"hub {hub}: its drives sit behind {len(k)} different PC ports")
        flat = [next(iter(v)) for v in keys.values() if len(v) == 1]
        if len(flat) != len(set(flat)):
            warnings.append("both hubs sit behind the same PC port (daisy-chained?)")
        if policies - {3, None}:
            warnings.append(f"removal policies differ between drives: {sorted(map(str, policies))}")
        if not self.platform.is_admin():
            warnings.append("no administrator rights: no device restart in the recovery protocol")
        for chk in self.checks:
            if chk.get("status") == "fail":
                warnings.append(f"host check failed: {chk['title']} ({chk['detail']})")
        selftest = read_json(self.cstore.host_dir / "selftest.json")
        if not selftest or not selftest.get("passed"):
            warnings.append("the self-test on a spare drive has not passed yet")
        return {"ready": not problems, "problems": problems, "warnings": warnings}

    def start_test(self, force: bool = False) -> dict[str, Any]:
        with self.lock_state:
            if self.run.get("phase") != "setup":
                return {"ok": False, "error": f"the test is already {self.run.get('phase')}"}
            ready = self.readiness()
            if ready["problems"] and not (force and self.sim_root):
                return {"ok": False, **ready}
            if ready["warnings"] and not force:
                return {"ok": False, "needs_confirmation": True, **ready}
            now = utc_now()
            atomic_write_json(self.cstore.campaign_json, self.campaign.to_json())
            self.run = {"phase": "running", "started_at": iso(now),
                        "stop_at": iso(now + timedelta(hours=self.campaign.duration_h)),
                        "duration_h": self.campaign.duration_h, "software": __version__,
                        "platform": self.platform.name, "started_warnings": ready["warnings"],
                        "history": [{"time": iso(now), "phase": "running"}]}
            self.cstore.save_run_state(self.run)
            self.cstore.host_event("test_start", stop_at=self.run["stop_at"], warnings=ready["warnings"])
            self.cstore.operator("start", warnings=ready["warnings"])
            self._schedule_starts(time.monotonic() + 1.0, self.campaign.stagger_s)
            return {"ok": True, "started_at": self.run["started_at"], "stop_at": self.run["stop_at"]}

    def end_now(self, reason: str) -> dict[str, Any]:
        with self.lock_state:
            if self.run.get("phase") not in ("running", "paused"):
                return {"ok": False, "error": "the test is not running"}
            self.run["stop_at"] = iso()
            self.run["ended_early"] = {"time": iso(), "reason": reason}
            self.cstore.save_run_state(self.run)
            self.cstore.operator("end_now", reason=reason)
            if self.run.get("phase") == "paused":
                self._censor_stragglers()
                self._set_phase("stopped", reason="ended while paused")
            else:
                self._set_phase("stopping", reason=f"ended early: {reason}")
            return {"ok": True}

    def pause(self, reason: str) -> dict[str, Any]:
        with self.lock_state:
            if self.run.get("phase") != "running":
                return {"ok": False, "error": "the test is not running"}
            self._stop_all_workers(f"paused: {reason}")
            self._set_phase("pausing", reason=reason)
            self.cstore.operator("pause", reason=reason)
            return {"ok": True}

    def resume(self) -> dict[str, Any]:
        with self.lock_state:
            if self.run.get("phase") != "paused":
                return {"ok": False, "error": "the test is not paused"}
            for unit in self.procs:
                self.cstore.drive(unit).clear_control()
            self._set_phase("running", reason="resumed by the operator")
            self._schedule_starts(time.monotonic() + 1.0, 1.0 if self.sim_root else 5.0)
            self.cstore.operator("resume")
            return {"ok": True}

    def finish(self) -> dict[str, Any]:
        if self.run.get("phase") != "stopped":
            return {"ok": False, "error": "finish after the post-test protocol, when the test is stopped"}
        self._set_phase("finished")
        self.submit("summary", {})
        return {"ok": True}

    def note(self, text: str, drive: str = "") -> dict[str, Any]:
        text = text.strip()[:2000]
        if not text:
            return {"ok": False, "error": "empty note"}
        if drive and drive in self.campaign.all_units():
            state = self.cstore.drive(drive).load_state() or {}
            self.cstore.drive(drive).note(state.get("cycles_done", 0), text=text, by="operator")
        self.cstore.operator("note", drive=drive, text=text)
        self.cstore.host_event("note", drive=drive, text=text)
        return {"ok": True}

    def add_temperature(self, celsius: float, location: str, note: str = "") -> dict[str, Any]:
        if not -20 <= celsius <= 120:
            return {"ok": False, "error": "temperature out of range"}
        append_csv(self.cstore.host_dir / "temperature.csv", TEMPERATURE_FIELDS,
                   {"time": iso(), "source": "manual", "location": location[:40], "celsius": celsius,
                    "note": note[:200]})
        self.cstore.operator("temperature", celsius=celsius, location=location)
        return {"ok": True}

    def request_shutdown(self) -> dict[str, Any]:
        self.cstore.operator("shutdown", phase=self.run.get("phase"))
        self.shutdown_requested.set()
        return {"ok": True}

    # -- jobs -----------------------------------------------------------------------------
    def submit(self, kind: str, args: dict[str, Any]) -> dict[str, Any]:
        job = Job(id=secrets.token_hex(4), kind=kind, args=args)
        with self.lock_state:
            self.jobs.append(job)
            self.jobs = self.jobs[-60:]
        self.job_queue.put(job)
        return job.to_json()

    def _job_loop(self) -> None:
        while True:
            job = self.job_queue.get()
            job.status, job.started = "running", iso()
            try:
                job.result = self._run_job(job)
                job.status = "done"
            except Exception as exc:
                job.status = "error"
                job.error = posttest.unsupported_message(exc)
                self._log(f"job {job.kind} failed: {traceback.format_exc()}")
            job.ended = iso()
            self._t["snapshot"] = 0.0

    def _run_job(self, job: Job) -> Any:
        a, c, cs, p = job.args, self.campaign, self.cstore, self.platform

        def progress(text: str) -> None:
            job.progress = text

        phase = self.run.get("phase")
        if job.kind == "enroll":
            if phase != "setup":
                raise RuntimeError("drives can only be enrolled before the test starts")
            return {k: v for k, v in intake.enroll(c, cs, p, a["drive"], guid=a.get("guid", ""),
                                                   root=a.get("root", ""), serial=a.get("serial", ""),
                                                   progress=progress).items()
                    if k in ("drive_id", "usb_serial", "usable_bytes", "warnings", "hub_key", "removal_policy")}
        if job.kind == "selftest":
            return posttest.selftest(c, cs, p, a["drive"], progress=progress)
        if job.kind in ("posttest", "recheck"):
            if phase in ("running", "stopping", "pausing") and a["drive"] in self.procs:
                status = (cs.drive(a["drive"]).load_state() or {}).get("status")
                if status not in FINAL:
                    raise RuntimeError("post-test checks run after the stop (or on stopped drives)")
            return posttest.post_check(c, cs, p, a["drive"], kind=job.kind, progress=progress)
        if job.kind == "portcheck":
            return posttest.port_check(c, cs, p, a["drive"], progress=progress)
        if job.kind == "retire":
            return posttest.retire(cs, a["drive"], a["classification"], a.get("reason", ""))
        if job.kind == "checks":
            self.checks = hostops.run_checks(p, self.package_dir, self.data_dir, len(c.test_drives()))
            atomic_write_json(cs.host_dir / "checks.json", self.checks)
            return {"checks": len(self.checks)}
        if job.kind == "fix":
            result = hostops.apply_fix(cs, a["check"])
            self.checks = hostops.run_checks(p, self.package_dir, self.data_dir, len(c.test_drives()))
            atomic_write_json(cs.host_dir / "checks.json", self.checks)
            return result
        if job.kind == "undo":
            return hostops.undo_all(cs)
        if job.kind == "inventory":
            return self._refresh_inventory()
        if job.kind == "host_info":
            info = p.host_info()
            info["package_dir"] = self.package_dir
            atomic_write_json(cs.host_dir / "host_info.json", info)
            return {"node": info.get("node")}
        if job.kind == "summary":
            return export.write_summary(c, cs)
        if job.kind == "export":
            return export.export_zip(c, cs, self.exports_dir)
        if job.kind == "sim_unplug" and self.sim_root:
            serial = self._sim_serial(a["drive"])
            p.unplug(serial, float(a.get("seconds", 20)))  # type: ignore[attr-defined]
            return {"unplugged": a["drive"]}
        if job.kind == "sim_replug" and self.sim_root:
            p.replug(self._sim_serial(a["drive"]))  # type: ignore[attr-defined]
            return {"replugged": a["drive"]}
        if job.kind == "sim_move" and self.sim_root:
            p.move(self._sim_serial(a["drive"]), a["hub"], int(a["port"]))  # type: ignore[attr-defined]
            self.submit("inventory", {})
            return {"moved": a["drive"], "to": f"{a['hub']}{a['port']}"}
        if job.kind == "sim_hub" and self.sim_root:
            return {"hit": p.hub_glitch(a["hub"], float(a.get("seconds", 30)))}  # type: ignore[attr-defined]
        if job.kind == "sim_enroll_all" and self.sim_root:
            return self._sim_enroll_all(progress)
        raise RuntimeError(f"unknown job {job.kind}")

    def _sim_serial(self, unit: str) -> str:
        for dev in self.platform.devices():  # type: ignore[attr-defined]
            if dev.get("hint") == unit:
                return dev["serial"]
        raise RuntimeError(f"no simulated device for {unit}")

    def _sim_enroll_all(self, progress) -> dict[str, Any]:
        cands = {cnd["serial"]: cnd for cnd in intake.candidates(self.campaign, self.cstore, self.platform)}
        done = []
        for unit in list(self.campaign.spares) + self.campaign.test_drives():
            if self.cstore.drive(unit).load_intake():
                continue
            serial = self._sim_serial(unit)
            cand = cands.get(serial)
            if cand is None:
                continue
            progress(f"enrolling {unit}")
            intake.enroll(self.campaign, self.cstore, self.platform, unit, guid=cand["guid"], serial=serial)
            done.append(unit)
        self._update_presence()
        return {"enrolled": done}

    def _inventory_loop(self) -> None:
        while not self.shutdown_requested.is_set():
            try:
                if self.platform.supports_inventory:
                    self._refresh_inventory()
            except Exception as exc:
                self._log(f"inventory failed: {exc}")
            if self.campaign.temperature.acpi and sys.platform == "win32":
                self._acpi_temperature()
            self.shutdown_requested.wait(INVENTORY_S if self.run.get("phase") == "running" else 60.0)

    def _refresh_inventory(self) -> dict[str, Any]:
        disks = self.platform.inventory()
        self.inventory = {"time": iso(), "disks": disks}
        atomic_write_json(self.cstore.host_dir / "inventory.json", self.inventory)
        return {"disks": len(disks)}

    def _acpi_temperature(self) -> None:
        from flashrel.platform.windows import run_powershell

        try:
            temps = run_powershell("$t = @(Get-CimInstance -Namespace root/wmi -ClassName "
                                   "MSAcpi_ThermalZoneTemperature -ErrorAction Stop | ForEach-Object "
                                   "{ [math]::Round($_.CurrentTemperature / 10 - 273.15, 1) }); "
                                   "ConvertTo-Json -Compress -InputObject @($t)", timeout=30)
        except Exception:
            return
        for i, value in enumerate(temps or []):
            if isinstance(value, (int, float)) and 0 < value < 120:
                append_csv(self.cstore.host_dir / "temperature.csv", TEMPERATURE_FIELDS,
                           {"time": iso(), "source": "acpi", "location": f"PC thermal zone {i}",
                            "celsius": value, "note": ""})

    # ======================================================================================
    # snapshot for the dashboard
    # ======================================================================================
    def _sample_hubs(self, now: float) -> None:
        sample: dict[str, dict[str, float]] = {h: {"active": 0, "writing": 0, "verifying": 0, "deleting": 0,
                                                   "mbps": 0.0, "w": 0.0, "r": 0.0} for h in self.campaign.hubs}
        for unit in self.campaign.test_drives():
            slot = self.campaign.slot_of(unit)
            live = self.cstore.drive(unit).load_live() or {}
            proc = self.procs.get(unit)
            if not slot or not proc or proc.popen is None:
                continue
            s = sample[slot.hub]
            s["active"] += 1
            ph, rate = live.get("phase"), float(live.get("rate_mbps") or 0)
            if ph == "write":
                s["writing"] += 1
                s["w"] += rate
            elif ph == "verify":
                s["verifying"] += 1
                s["r"] += rate
            elif ph == "delete":
                s["deleting"] += 1
            if ph in ("write", "verify", "check"):
                s["mbps"] += rate
        for hub, s in sample.items():
            self.hub_series[hub].append([iso(), round(s["mbps"], 2)])
            self._hub_acc[hub].append(s)
        if now - self._t["hubw"] >= HUB_WRITE_S and self.run.get("phase") == "running":
            self._t["hubw"] = now
            for hub, items in self._hub_acc.items():
                if not items:
                    continue
                n = len(items)
                append_csv(self.cstore.host_dir / "hub_load.csv", HUB_FIELDS, {
                    "time": iso(), "hub": hub, "drives_active": max(i["active"] for i in items),
                    "writing": round(sum(i["writing"] for i in items) / n, 2),
                    "verifying": round(sum(i["verifying"] for i in items) / n, 2),
                    "deleting": round(sum(i["deleting"] for i in items) / n, 2),
                    "mbps_total": round(sum(i["mbps"] for i in items) / n, 3),
                    "mbps_write": round(sum(i["w"] for i in items) / n, 3),
                    "mbps_read": round(sum(i["r"] for i in items) / n, 3)})
                self._hub_acc[hub] = []

    def _read_new_events(self, unit: str) -> None:
        ds = self.cstore.drive(unit)
        for path in (ds.events_jsonl, ds.dir / "notes.jsonl"):
            key = f"{unit}:{path.name}"
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            offset = self._event_offsets.get(key, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(size - offset)
            except OSError:
                continue  # held by another program for a moment: next time
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self._event_offsets[key] = offset + end + 1
            dq = self.events.setdefault(unit, collections.deque(maxlen=300))
            for line in chunk[: end + 1].splitlines():
                try:
                    dq.append(json.loads(line))
                except ValueError:
                    continue

    def _hours_now(self) -> float:
        started = self.run.get("started_at")
        if not started:
            return 0.0
        end = utc_now()
        if self.run.get("stop_at") and self.run.get("phase") in ("stopped", "finished"):
            end = min(end, parse_iso(self.run["stop_at"]))
        return max(0.0, (end - parse_iso(started)).total_seconds() / 3600)

    def _survival(self) -> dict[str, Any]:
        rows_c, rows_h = [], []
        hours_now = self._hours_now()
        for unit in self.campaign.test_drives():
            state = self.cstore.drive(unit).load_state() or {}
            if not state.get("started_at"):
                continue
            group = self.campaign.group_of(unit).id
            first = state.get("first_failure") if not (state.get("first_failure") or {}).get("attributed") else None
            if first:
                rows_c.append({"group": group, "t": max(0.5, first["cycle"]), "event": 1, "drive": unit})
                rows_h.append({"group": group, "t": max(0.01, first.get("hours_on_test") or hours_now), "event": 1})
            else:
                rows_c.append({"group": group, "t": max(0.5, state.get("cycles_done", 0)), "event": 0, "drive": unit})
                rows_h.append({"group": group, "t": max(0.01, hours_now), "event": 0})
        return {"cycles": analysis.group_summary(rows_c, "cycles"),
                "hours": analysis.group_summary(rows_h, "hours")}

    def _build_snapshot(self) -> dict[str, Any]:
        c = self.campaign
        drives: dict[str, Any] = {}
        kpi = collections.Counter()
        fails_by_group: dict[str, list[int]] = {g: [0, len([u for u in c.test_drives() if c.group_of(u).id == g])]
                                                for g in c.groups}
        tb_w = tb_r = 0.0
        cycles_total = failed_cycles = 0
        inv_by_serial = {d.get("serial"): d for d in (self.inventory or {}).get("disks", [])}
        for unit in c.all_units():
            role = c.role(unit)
            if role == "unused":
                continue
            ds = self.cstore.drive(unit)
            rec = ds.load_intake() or {}
            state = ds.load_state() or {}
            live = ds.load_live() or {}
            pres = self.presence.get("drives", {}).get(unit, {})
            proc = self.procs.get(unit)
            self._read_new_events(unit)
            slot = c.slot_of(unit)
            status = state.get("status") or ("enrolled" if rec else "not enrolled")
            running = bool(proc and proc.popen is not None and proc.popen.poll() is None)
            hb_age = None
            if live.get("heartbeat"):
                try:
                    hb_age = round((utc_now() - parse_iso(live["heartbeat"])).total_seconds(), 1)
                except ValueError:
                    pass
            disk = inv_by_serial.get(rec.get("usb_serial")) if rec.get("serial_unique", True) else None
            moved = bool(disk and rec.get("hub_key") and disk.get("hub_key") and
                         (disk.get("hub_key"), disk.get("port_chain")) != (rec.get("hub_key"), rec.get("port_chain")))
            cum = state.get("cum") or {}
            usable = int(rec.get("usable_bytes") or 0)
            entry = {
                "id": unit, "group": c.group_of(unit).id, "role": role,
                "hub": slot.hub if slot else "", "port": slot.port if slot else 0,
                "enrolled": bool(rec), "status": status, "present": pres.get("present"),
                "found_by": pres.get("how"), "letter": pres.get("letter") or live.get("letter", ""),
                "worker": {"running": running, "pid": proc.popen.pid if running else None,
                           "restarts": len(proc.restarts) if proc else 0,
                           "last_exit": proc.last_exit if proc else None,
                           "giving_up": proc.giving_up if proc else False,
                           "hang": bool(proc and proc.hang)},
                "cycle": live.get("cycle") if running else state.get("cycles_done"),
                "workload": live.get("workload") if running else "",
                "phase": live.get("phase") if running else "",
                "files_done": live.get("files_done", 0), "files_total": live.get("files_total", 0),
                "bytes_done": live.get("bytes_done", 0), "bytes_total": live.get("bytes_total", 0),
                "rate_mbps": live.get("rate_mbps"), "rate_fps": live.get("rate_fps"),
                "phase_started": live.get("phase_started"), "heartbeat_age": hb_age,
                "cycles_done": state.get("cycles_done", 0), "failures": len(state.get("failure_cycles") or []),
                "recoveries": state.get("recoveries", 0), "transients": state.get("transients", 0),
                "first_failure": state.get("first_failure"), "end_of_life": state.get("end_of_life"),
                "last_outcome": state.get("last_outcome", ""), "last_mbps": state.get("last_mbps") or {},
                "last_sentinel": live.get("last_sentinel"), "fdw": round(cum.get("bytes_written", 0) / usable, 3) if usable else 0,
                "bytes_written": cum.get("bytes_written", 0),
                "file_ops": cum.get("files_created", 0) + cum.get("files_deleted", 0),
                "usable_bytes": usable, "removal_policy": rec.get("removal_policy"),
                "raw_checks": bool((rec.get("exfat") or {}).get("ok")), "moved": moved,
                "usb_serial": rec.get("usb_serial", ""), "warnings": rec.get("warnings", []),
                "slow": state.get("slow_flagged") or [], "quarantined": state.get("quarantined") or [],
            }
            drives[unit] = entry
            if role == "test":
                kpi[status] += 1
                if rec and not pres.get("present") and status not in FINAL:
                    kpi["missing"] += 1
                tb_w += cum.get("bytes_written", 0) / 1e12
                tb_r += cum.get("bytes_read", 0) / 1e12
                cycles_total += state.get("cycles_done", 0)
                failed_cycles += len(state.get("failure_cycles") or [])
                if state.get("first_failure") and not state["first_failure"].get("attributed"):
                    fails_by_group[entry["group"]][0] += 1
        merged = [e for dq in self.events.values() for e in dq]
        merged.sort(key=lambda e: e.get("time", ""))
        noisy = {"sentinel_warning"}
        feed = [e for e in merged if e.get("type") not in noisy][-120:]
        run = dict(self.run)
        elapsed = remaining = None
        if run.get("started_at"):
            end = utc_now()
            if run.get("stop_at") and run.get("phase") in ("stopping", "stopped", "finished"):
                end = min(end, parse_iso(run["stop_at"]))  # the clock stops with the test
            elapsed = (end - parse_iso(run["started_at"])).total_seconds()
            if run.get("stop_at"):
                remaining = max(0.0, (parse_iso(run["stop_at"]) - utc_now()).total_seconds())
        run.pop("history", None)
        host_info = read_json(self.cstore.host_dir / "host_info.json", {}) or {}
        with self.lock_state:
            jobs = [j.to_json() for j in self.jobs[-25:]]
        hub_keys: dict[str, list[str]] = {}
        for unit in c.test_drives():
            rec = self.cstore.drive(unit).load_intake() or {}
            if rec.get("hub_key"):
                hub_keys.setdefault(rec["hub"], [])
                if rec["hub_key"] not in hub_keys[rec["hub"]]:
                    hub_keys[rec["hub"]].append(rec["hub_key"])
        return {
            "now": iso(), "version": __version__, "platform": self.platform.name, "sim": bool(self.sim_root),
            "token_hint": None,
            "campaign": {"name": c.name, "title": c.title, "duration_h": c.duration_h, "hubs": list(c.hubs),
                         "ports": c.ports_per_hub, "rotation": list(c.cycle.rotation), "seed": c.seed,
                         "hub_model": c.hub_model, "spares": list(c.spares),
                         "workloads": {k: {"min": w.min_size, "max": w.max_size} for k, w in c.workloads.items()},
                         "groups": {g.id: {"brand": g.brand, "model": g.model, "capacity_gb": g.capacity_gb,
                                           "n": fails_by_group[g.id][1], "color": GROUP_COLORS.get(g.id, "#64748b")}
                                    for g in c.groups.values()},
                         "slots": [{"hub": s.hub, "port": s.port, "drive": s.drive} for s in c.slots]},
            "run": {**run, "elapsed_s": elapsed, "remaining_s": remaining},
            "host": {"admin": self.platform.is_admin(), "node": host_info.get("node", ""),
                     "model": " ".join(x for x in (host_info.get("manufacturer"), host_info.get("model")) if x),
                     "os": host_info.get("os_caption") or host_info.get("os", ""),
                     "cpu": host_info.get("cpu", ""), "python": host_info.get("python", ""),
                     "checks": self.checks, "hub_keys": hub_keys,
                     "inventory_time": (self.inventory or {}).get("time")},
            "kpi": {"test_drives": len(c.test_drives()), "by_status": dict(kpi), "cycles_total": cycles_total,
                    "failed_cycles": failed_cycles, "tb_written": round(tb_w, 3), "tb_read": round(tb_r, 3),
                    "failures_by_group": fails_by_group},
            "drives": drives, "events": feed, "survival": self.survival,
            "hub_load": {h: list(self.hub_series[h])[-360:] for h in c.hubs},
            "jobs": jobs, "readiness": self.readiness() if self.run.get("phase") == "setup" else None,
        }

    def snapshot_json(self) -> dict[str, Any]:
        with self.lock_state:
            if not self.snapshot:
                self.snapshot = self._build_snapshot()
            snap = dict(self.snapshot)
            snap["jobs"] = [j.to_json() for j in self.jobs[-25:]]  # always current
            snap["run"] = {**snap.get("run", {}), "phase": self.run.get("phase")}
            return snap

    def drive_detail(self, unit: str) -> dict[str, Any]:
        if unit not in self.campaign.all_units():
            raise KeyError(unit)
        ds = self.cstore.drive(unit)
        from flashrel.store import read_csv

        return {"id": unit, "intake": _public_intake(ds.load_intake()), "state": ds.load_state(),
                "live": ds.load_live(), "cycles": ds.read_cycles(), "events": ds.read_events()[-400:],
                "sentinel": read_csv(ds.sentinel_csv)[-60:], "trace": read_csv(ds.trace_csv)[-600:]}

    def setup_info(self) -> dict[str, Any]:
        return {"candidates": intake.candidates(self.campaign, self.cstore, self.platform),
                "readiness": self.readiness(), "selftest": read_json(self.cstore.host_dir / "selftest.json"),
                "posttest": read_json(self.cstore.host_dir / "posttest.json", {}),
                "platform": {"name": self.platform.name, "format": self.platform.supports_format,
                             "admin": self.platform.is_admin(), "sim": bool(self.sim_root)}}


def _public_intake(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rec:
        return rec
    out = dict(rec)
    out.pop("identity_b64", None)
    if isinstance(out.get("exfat"), dict) and "record" in out["exfat"]:
        out["exfat"] = {"ok": out["exfat"].get("ok"),
                        "geometry": out["exfat"]["record"].get("geometry"),
                        "files": out["exfat"]["record"].get("files")}
    return out
