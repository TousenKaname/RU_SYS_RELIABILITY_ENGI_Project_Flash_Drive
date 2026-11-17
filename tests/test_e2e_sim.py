"""End to end on simulated drives: intake, worker, resume after a crash, controller run."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import timedelta

import pytest

from flashrel.controller import Controller
from flashrel.failure import Dose
from flashrel.intake import IntakeError, candidates, enroll
from flashrel.platform.sim import SimPlatform
from flashrel.store import CampaignStore, atomic_write_json, read_json
from flashrel.timeutil import iso, utc_now
from flashrel.worker import Worker

from conftest import CONFIG


def _rig(tmp_path, campaign, units):
    plat = SimPlatform(str(tmp_path / "sim"), speed=1000)
    plat.create_rig(campaign, seed=5)
    cs = CampaignStore(tmp_path / "data", campaign.name)
    by_hint = {d["hint"]: d for d in plat.devices()}
    cands = {c["serial"]: c for c in candidates(campaign, cs, plat)}
    for unit in units:
        dev = by_hint[unit]
        plat.update_device(dev["serial"], fail_after_ops=None)  # no random faults unless a test asks
        cand = cands[dev["serial"]]
        enroll(campaign, cs, plat, unit, guid=cand["guid"], serial=dev["serial"])
    return plat, cs, by_hint


def _running(cs, campaign, seconds):
    atomic_write_json(cs.campaign_json, campaign.to_json())
    now = utc_now()
    cs.save_run_state({"phase": "running", "started_at": iso(now), "stop_at": iso(now + timedelta(seconds=seconds))})
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            atomic_write_json(cs.controller_json, {"time": iso()})
            stop.wait(1.0)
    threading.Thread(target=beat, daemon=True).start()
    return stop


def test_intake_guards_against_the_wrong_stick(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, [])
    cands = {c["serial"]: c for c in candidates(sim_campaign, cs, plat)}
    small = cands[by_hint["A8-06"]["serial"]]
    with pytest.raises(IntakeError, match="does not fit"):
        enroll(sim_campaign, cs, plat, "A16-10", guid=small["guid"], serial=small["serial"])
    rec = enroll(sim_campaign, cs, plat, "A8-06", guid=small["guid"], serial=small["serial"])
    assert rec["usable_bytes"] > 0 and rec["hub_key"].endswith("USB(1)")
    with pytest.raises(IntakeError, match="already enrolled"):
        enroll(sim_campaign, cs, plat, "A8-07", guid=small["guid"], serial=small["serial"])


def test_worker_runs_cycles_and_censors_at_the_stop(tmp_path, sim_campaign):
    plat, cs, _ = _rig(tmp_path, sim_campaign, ["S8-01"])
    stop = _running(cs, sim_campaign, 8)
    assert Worker(sim_campaign, cs, "S8-01", plat).main() == 0
    stop.set()
    st = cs.drive("S8-01").load_state()
    assert st["status"] == "censored" and st["cycles_done"] >= 3
    rows = cs.drive("S8-01").read_cycles()
    assert [r["workload"] for r in rows[:3]] == ["large", "medium", "small"]
    assert all(r["outcome"] == "pass" for r in rows)
    assert int(rows[-1]["cum_files_created"]) > 0 and float(rows[-1]["cum_fdw"]) > 0
    events = cs.drive("S8-01").read_events()
    assert events[-1]["type"] == "censored"
    # a cycle cut short by the stop (if the stop fell inside one) is logged, not counted
    cut = [e for e in events if e["type"] == "interrupted"]
    assert all(e["detail"]["at_stop_time"] for e in cut) and len(cut) <= 1


def test_metadata_fault_is_found_and_the_drive_recovers(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, ["A8-06"])
    plat.update_device(by_hint["A8-06"]["serial"], fail_after_ops=150, mode="metadata")
    stop = _running(cs, sim_campaign, 10)
    Worker(sim_campaign, cs, "A8-06", plat).main()
    stop.set()
    st = cs.drive("A8-06").load_state()
    assert st["first_failure"]["code"] == "F1"
    dose = st["first_failure"]["dose"]  # the failure is time-stamped by dose (creates + deletes)
    assert dose["files_created"] + dose["files_deleted"] >= 150 - 2
    assert st["recoveries"] >= 1 and st["status"] == "censored"
    assert set(st["sentinel_skip"]) & {"identity", "sentinel"}


def test_corrupting_drive_ends_in_attention(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, ["A16-10"])
    plat.update_device(by_hint["A16-10"]["serial"], fail_after_ops=20, mode="corrupt")
    stop = _running(cs, sim_campaign, 30)
    Worker(sim_campaign, cs, "A16-10", plat).main()
    stop.set()
    st = cs.drive("A16-10").load_state()
    assert st["status"] == "attention" and st["end_of_life"]["code"] == "F1"
    ev = [e["type"] for e in cs.drive("A16-10").read_events()]
    assert ev.index("cycle_failure") < ev.index("device_reset") < ev.index("attention")


def test_resume_after_a_crash_logs_the_partial_wear(tmp_path, sim_campaign):
    plat, cs, _ = _rig(tmp_path, sim_campaign, ["S8-02"])
    ds = cs.drive("S8-02")
    state = ds.load_state()
    state.update({"status": "active", "cycles_done": 4, "in_progress": {"cycle": 5, "workload": "medium"},
                  "cum": Dose(bytes_written=1000).to_json()})
    ds.save_state(state)
    ds.save_live({"cycle_dose": Dose(bytes_written=777, files_created=3).to_json(), "phase": "write"})
    stop = _running(cs, sim_campaign, 1)
    Worker(sim_campaign, cs, "S8-02", plat).main()
    stop.set()
    first = next(e for e in ds.read_events() if e["type"] == "interrupted")
    assert first["cycle"] == 5 and first["detail"]["estimated_dose"]["bytes_written"] == 777
    assert ds.load_state()["cum"]["bytes_written"] >= 1777


@pytest.mark.slow
def test_controller_runs_a_whole_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(CONFIG.parent.parent / "src"))
    plat = SimPlatform(str(tmp_path / "sim"), speed=300)
    ctrl = Controller(config_path=str(CONFIG), data_dir=str(tmp_path / "data"), exports_dir=str(tmp_path / "exp"),
                      platform=plat, package_dir=str(CONFIG.parent.parent), sim_root=str(tmp_path / "sim"),
                      sim_speed=300, sim_hours=25 / 3600)
    try:
        ctrl.start_background()  # heartbeat, presence scan, jobs
        ctrl._run_job(type("J", (), {"args": {}, "kind": "sim_enroll_all", "progress": ""})())
        ctrl._update_presence()
        assert ctrl.readiness()["ready"]
        assert ctrl.start_test(force=True)["ok"]
        t0 = time.monotonic()
        while ctrl.run["phase"] != "stopped" and time.monotonic() - t0 < 150:
            ctrl.tick()
            time.sleep(0.5)
        assert ctrl.run["phase"] == "stopped"
        states = [ctrl.cstore.drive(d).load_state()["status"] for d in ctrl.campaign.test_drives()]
        assert all(s in ("censored", "attention", "failed") for s in states)
        assert sum(ctrl.cstore.drive(d).load_state()["cycles_done"] for d in ctrl.campaign.test_drives()) >= 20
        snap = ctrl._build_snapshot()
        assert snap["kpi"]["test_drives"] == 20 and json.dumps(snap)
    finally:
        ctrl.shutdown_requested.set()
        ctrl._stop_all_workers("test over", wait=30)
        ctrl.lock.release()


def test_append_never_fails_when_the_file_is_held(tmp_path, monkeypatch):
    """Excel locks a CSV it has open: the record is parked and merged later, in order."""
    from flashrel import store

    path = tmp_path / "cycles.csv"
    store.append_csv(path, ("a",), {"a": 1})
    real_open = open
    held = {"on": True}

    def fake_open(file, mode="r", *args, **kwargs):
        if held["on"] and str(file) == str(path) and ("a" in mode or "+" in mode):
            raise PermissionError(13, "held by another program", str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    store.append_csv(path, ("a",), {"a": 2})            # parked
    assert [r["a"] for r in store.read_csv(path)] == ["1", "2"]  # readers see it already
    held["on"] = False
    store.append_csv(path, ("a",), {"a": 3})            # merges the parked row first
    assert [r["a"] for r in store.read_csv(path)] == ["1", "2", "3"]
    assert not (tmp_path / "cycles.csv.pending").exists()
