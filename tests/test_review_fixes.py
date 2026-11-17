"""Regression tests for the defects found in the review of FlashRel Rig 2 (November 2026).

Each test reproduces one way the rig used to book a wrong life record (a failure lost, a
failure counted that was not the drive's, a phantom cycle) or touch the wrong stick.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import threading
import time
from datetime import timedelta

import pytest

from flashrel import exfat
from flashrel.config import CycleSpec, SentinelSpec
from flashrel.controller import Controller, JobRefused, Proc
from flashrel.cycle import CycleRunner, Hooks
from flashrel.failure import Dose, Outcome
from flashrel.intake import IntakeError, candidates, enroll
from flashrel.platform.base import FileRaw, Platform, StorageInfo, Volume
from flashrel.platform.sim import SimPlatform
from flashrel.posttest import _leftover_cycle
from flashrel.sentinel import SentinelChecker, locate
from flashrel.store import CampaignStore, DriveStore, atomic_write_json
from flashrel.timeutil import iso, utc_now
from flashrel.worker import Live, Worker

from conftest import CONFIG
from test_e2e_sim import _rig, _running
from test_engine import TmpPlatform, W, _intake, _runner
from flashrel.payload import fill_budget, plan_cycle

ID, SENT = "FLASHREL-ID.json", "FLASHREL-SENTINEL.bin"


# -- the cycle engine ---------------------------------------------------------------------------
def _engine(tmp_path, platform, *, sleep=lambda s: None, **hooks):
    def planner(free, cluster):
        return plan_cycle(seed=1, drive_id="T-01", cycle=1, workload=W,
                          budget_bytes=fill_budget(free, 0.5, 2**20), cluster_size=cluster, chunk_size=2**20)

    return CycleRunner(platform, str(tmp_path), planner, CycleSpec(retry_backoff_s=0.0), drive_id="T-01",
                       cycle=1, workload="medium", hooks=Hooks(**hooks), sleep=sleep)


def test_corruption_found_before_a_stop_stays_f1(tmp_path):
    def flip(cycle_dir, plan):
        path = os.path.join(cycle_dir, *plan.files[2].relpath.split("/"))
        with open(path, "r+b") as fh:
            fh.seek(100)
            fh.write(b"\x00" if fh.read(1) != b"\x00" else b"\x01")

    deleted = {"n": 0}

    def progress(phase, files, nbytes):
        if phase == "delete":
            deleted["n"] += files

    r = _runner(tmp_path, after_write=flip, progress=progress, should_stop=lambda: deleted["n"] > 2).run()
    assert r.outcome is Outcome.CORRUPTION and r.files_corrupt == 1 and r.phase == "delete"
    assert any(e.kind == "stopped" for e in r.errors)


def test_stop_during_a_retry_backoff_is_not_an_error(tmp_path):
    class BusyOnce(TmpPlatform):
        busy = True

        def remove_file(self, path):
            if self.busy:
                self.busy = False
                raise OSError(errno.EBUSY, "held by a scanner", path)
            os.remove(path)

    stop = threading.Event()
    r = _engine(tmp_path, BusyOnce(), sleep=lambda s: stop.set(), should_stop=stop.is_set).run()
    assert r.outcome is Outcome.ABORTED  # not a counted I/O error
    assert any("cut short by a stop" in e.detail for e in r.errors)


def test_reread_error_keeps_the_mismatch(tmp_path):
    class FlipThenFail(TmpPlatform):
        target, reads = "", 0

        def read_chunks(self, path, chunk_size, *, uncached=True):
            if os.path.basename(path) == self.target:
                self.reads += 1
                if self.reads > 1:
                    raise OSError(errno.EIO, "Input/output error", path)
                data = bytearray(b"".join(super().read_chunks(path, chunk_size, uncached=uncached)))
                data[50] ^= 0x01
                return iter([bytes(data)])
            return super().read_chunks(path, chunk_size, uncached=uncached)

    plat = FlipThenFail()

    def pick(cycle_dir, plan):
        plat.target = os.path.basename(plan.files[1].relpath)

    r = _engine(tmp_path, plat, after_write=pick).run()
    assert r.outcome is Outcome.CORRUPTION and r.files_corrupt == 1  # F1, not F2


def test_file_lost_after_the_readback_is_missing_not_deleted(tmp_path):
    seen = {}

    def keep(cycle_dir, plan):
        seen["dir"], seen["plan"] = cycle_dir, plan

    def periodic(phase):
        if phase == "verify-end":
            os.remove(os.path.join(seen["dir"], *seen["plan"].files[1].relpath.split("/")))

    r = _runner(tmp_path, after_write=keep, periodic=periodic).run()
    assert r.outcome is Outcome.CORRUPTION and r.files_missing == 1
    assert r.dose.files_deleted == r.planned_files - 1


# -- raw file-system checks ---------------------------------------------------------------------
def test_damaged_fat_offset_does_not_crash_the_check(exfat_image):
    with FileRaw(str(exfat_image)) as raw:
        base = exfat.baseline(raw, identity_name=ID, sentinel_name=SENT)
    with open(exfat_image, "r+b") as fh:  # the top byte of FatOffset (bytes 80-83)
        fh.seek(83)
        b = fh.read(1)
        fh.seek(83)
        fh.write(bytes([b[0] ^ 0x40]))
    with FileRaw(str(exfat_image)) as raw:
        result = exfat.check(raw, base)
    assert not result["boot"]["ok"]
    assert result["upcase"]["ok"] and result["rootdir"]["ok"]  # read where they were at intake


class _FailingRaw(FileRaw):
    def __init__(self, path, bad_from, bad_to):
        super().__init__(path)
        self.bad = (bad_from, bad_to)

    def read(self, offset, length):
        if offset < self.bad[1] and offset + length > self.bad[0]:
            raise OSError(errno.EIO, "data error (cyclic redundancy check)")
        return super().read(offset, length)


def test_unreadable_root_directory_is_a_finding(exfat_image, tmp_path):
    with FileRaw(str(exfat_image)) as raw:
        base = exfat.baseline(raw, identity_name=ID, sentinel_name=SENT)
        boot = exfat.parse_boot(raw.read(0, 512))
    start = boot.cluster_offset(boot.root_cluster)
    bad = (start, start + boot.cluster_size)
    with _FailingRaw(str(exfat_image), *bad) as raw:
        result = exfat.check(raw, base)
    assert result["boot"]["ok"] and result["upcase"]["ok"]
    assert result["rootdir"]["error"] and not result["rootdir"]["ok"]

    class RawPlatform(Platform):
        def raw_volume(self, vol):
            return _FailingRaw(str(exfat_image), *bad)

    vol_dir = tmp_path / "vol"
    vol_dir.mkdir()
    intake = {**_intake(vol_dir), "exfat": {"ok": True, "record": base}}
    checker = SentinelChecker(RawPlatform(), intake, SentinelSpec(confirm_reads=2, confirm_gap_s=0),
                              sleep=lambda s: None)
    res = checker.check(Volume(root=str(vol_dir) + os.sep, fs="exFAT"))
    assert not res.ok and res.failed == "rootdir" and res.code == "F2"  # not "raw check unavailable"


# -- the worker on simulated drives -------------------------------------------------------------
def test_a_stick_whose_file_system_goes_raw_is_f6_and_does_not_spin(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, ["A8-06"])
    plat.update_device(by_hint["A8-06"]["serial"], fail_after_ops=150, mode="raw")
    stop = _running(cs, sim_campaign, 25)
    Worker(sim_campaign, cs, "A8-06", plat).main()
    stop.set()
    st = cs.drive("A8-06").load_state()
    assert st["first_failure"]["code"] == "F6" and st["status"] == "attention"
    assert len(st["failure_cycles"]) == 1
    rows = (cs.drive("A8-06").sentinel_csv).read_text().count("\n")
    assert rows < 60  # no tight loop of start-of-cycle checks


def test_damage_to_several_records_is_counted_once(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, ["A8-07"])
    plat.update_device(by_hint["A8-07"]["serial"], fail_after_ops=150, mode="metadata")
    stop = _running(cs, sim_campaign, 12)
    Worker(sim_campaign, cs, "A8-07", plat).main()
    stop.set()
    st = cs.drive("A8-07").load_state()
    assert {"identity", "sentinel"} <= set(st["sentinel_skip"])
    assert len(st["failure_cycles"]) == 1 and st["status"] == "censored"
    # Run 1's damage hit four records at once: all four go on record from the one finding.
    w = Worker(sim_campaign, cs, "A8-07", plat)
    w._record_damage({"failed": "identity", "code": "F1",
                      "failed_all": ["identity", "sentinel", "upcase", "rootdir"]})
    assert w.state.sentinel_skip == ["identity", "sentinel", "upcase", "rootdir"]


def _workers(campaign, cs, sim_root, units):
    threads = []
    for unit in units:
        w = Worker(campaign, cs, unit, SimPlatform(sim_root, speed=1000))
        t = threading.Thread(target=w.main, daemon=True)
        t.start()
        threads.append(t)
    return threads


def _presence_writer(cs, plat, campaign, units, stop):
    """What the controller's presence scan writes, for the drives of the test."""
    def loop():
        while not stop.is_set():
            vols = plat.list_volumes()
            drives = {}
            for unit in units:
                vol, _ = locate(plat, unit, cs.drive(unit).load_intake(), volumes=vols)
                drives[unit] = {"present": vol is not None, "root": vol.root if vol else "",
                                "hub": campaign.slot_of(unit).hub, "expected": True}
            atomic_write_json(cs.presence_json, {"time": iso(), "drives": drives})
            stop.wait(0.5)
    threading.Thread(target=loop, daemon=True).start()


HUB_A = ["A16-10", "A16-05", "A8-06"]  # three drives on hub A


def test_a_hub_glitch_is_not_counted_against_the_drives(tmp_path, sim_campaign):
    plat, cs, _ = _rig(tmp_path, sim_campaign, HUB_A)
    stop = _running(cs, sim_campaign, 16)
    threads = _workers(sim_campaign, cs, plat.root, HUB_A)
    time.sleep(4)
    plat.hub_glitch("A", 3.0)
    for t in threads:
        t.join(60)
    stop.set()
    states = {u: cs.drive(u).load_state() for u in HUB_A}
    assert all(not s["failure_cycles"] and s["first_failure"] is None for s in states.values()), states
    assert all(s["status"] == "censored" for s in states.values())
    events = [e["type"] for u in HUB_A for e in cs.drive(u).read_events()]
    assert "hub_event" in events


def test_a_drive_that_does_not_come_back_with_its_hub_has_failed(tmp_path, sim_campaign):
    campaign = dataclasses.replace(sim_campaign, failure=dataclasses.replace(
        sim_campaign.failure, reconnect_timeout_s=4.0))
    plat, cs, by_hint = _rig(tmp_path, campaign, HUB_A)
    stop = _running(cs, campaign, 40)
    _presence_writer(cs, plat, campaign, HUB_A, stop)
    threads = _workers(campaign, cs, plat.root, HUB_A)
    time.sleep(4)
    plat.hub_glitch("A", 3.0)
    plat.unplug(by_hint["A8-06"]["serial"], None)  # this one never comes back
    for t in threads:
        t.join(90)
    stop.set()
    lost = cs.drive("A8-06").load_state()
    assert lost["status"] == "attention" and lost["first_failure"]["code"] == "F3"
    for unit in ("A16-10", "A16-05"):
        assert not cs.drive(unit).load_state()["failure_cycles"]


def test_a_held_state_file_is_never_replaced_by_a_fresh_one(tmp_path, sim_campaign, monkeypatch):
    from flashrel import store

    plat, cs, _ = _rig(tmp_path, sim_campaign, ["S8-01"])
    stop = _running(cs, sim_campaign, 5)
    ds = cs.drive("S8-01")
    ds.state_json.write_text('{"drive_id": "S8-01", "cycles_d')  # unreadable at this moment
    monkeypatch.setattr(store.time, "sleep", lambda s: None)
    assert Worker(sim_campaign, cs, "S8-01", plat).main() == 4
    stop.set()
    assert ds.state_json.read_text() == '{"drive_id": "S8-01", "cycles_d'


def test_a_failure_row_without_its_state_resumes_the_recovery(tmp_path, sim_campaign):
    plat, cs, _ = _rig(tmp_path, sim_campaign, ["S8-02"])
    ds = cs.drive("S8-02")
    state = ds.load_state()
    state.update({"status": "active", "cycles_done": 2, "in_progress": {"cycle": 3, "workload": "small"},
                  "cum": Dose(bytes_written=5000).to_json()})
    ds.save_state(state)
    ds.save_live({"cycle_dose": Dose(bytes_written=999).to_json(), "phase": "verify"})
    ds.append_cycle({"drive": "S8-02", "cycle": 3, "workload": "small", "outcome": "corruption", "code": "F1",
                     "ended_at": iso(), "cum_bytes_written": 9000, "hours_on_test": 1.5,
                     "errors": json.dumps([{"phase": "verify"}])})
    stop = _running(cs, sim_campaign, 6)
    Worker(sim_campaign, cs, "S8-02", plat).main()
    stop.set()
    st = ds.load_state()
    assert st["failure_cycles"][:1] == [3] and st["first_failure"]["code"] == "F1"
    assert st["first_failure"]["cycle"] == 3 and st["first_failure"]["hours_on_test"] == 1.5
    types = [(e["type"], e["cycle"]) for e in ds.read_events()]
    assert ("recovered", 3) in types and ("interrupted", 3) not in types  # no double-counted wear


def test_live_phase_changes_count_as_progress(tmp_path):
    live = Live(DriveStore(tmp_path, "X"), "X")
    live.update(phase="locating")
    b = live.data["beat"]
    live.update(phase="locating", status="active")
    assert live.data["beat"] == b
    live.update(phase="sentinel")
    live.beat()
    assert live.data["beat"] == b + 2


# -- the controller -----------------------------------------------------------------------------
@pytest.fixture
def ctrl(tmp_path):
    plat = SimPlatform(str(tmp_path / "sim"), speed=1000)
    c = Controller(config_path=str(CONFIG), data_dir=str(tmp_path / "data"), exports_dir=str(tmp_path / "exp"),
                   platform=plat, package_dir=str(CONFIG.parent.parent), sim_root=str(tmp_path / "sim"),
                   sim_speed=1000, sim_hours=0.01)
    by_hint = {d["hint"]: d for d in plat.devices()}
    cands = {x["serial"]: x for x in candidates(c.campaign, c.cstore, plat)}
    serial = by_hint["A16-10"]["serial"]
    enroll(c.campaign, c.cstore, plat, "A16-10", guid=cands[serial]["guid"], serial=serial)
    c.run = {"phase": "running", "started_at": iso(utc_now() - timedelta(hours=2))}
    yield c
    c.lock.release()


def _hang(ctrl, context, cycle, phase="write"):
    proc = Proc("A16-10")
    proc.hang = {"since": 0.0, "idle_s": 660, "phase": phase, "cycle": cycle, "context": context}
    ctrl._record_hang(proc)
    return ctrl.cstore.drive("A16-10")


def test_a_hang_in_a_cycle_carries_its_wear_and_hours(ctrl):
    ds = ctrl.cstore.drive("A16-10")
    state = ds.load_state()
    state.update({"status": "active", "cycles_done": 3, "in_progress": {"cycle": 4, "workload": "large"},
                  "cum": Dose(bytes_written=1000).to_json()})
    ds.save_state(state)
    ds.save_live({"cycle_dose": Dose(bytes_written=777).to_json(), "phase": "write", "cycle": 4})
    _hang(ctrl, "cycle", 4)
    st = ds.load_state()
    assert st["cycles_done"] == 4 and st["failure_cycles"] == [4] and st["cum"]["bytes_written"] == 1777
    assert st["status"] == "recovering" and st["pending_recovery"]["cycle"] == 4
    assert 1.9 < st["first_failure"]["hours_on_test"] < 2.1
    row = ds.read_cycles()[-1]
    assert row["cycle"] == "4" and row["code"] == "F3" and row["hours_on_test"] and row["cum_bytes_written"] == "1777"


def test_a_hang_in_the_recovery_protocol_books_no_new_cycle(ctrl):
    ds = ctrl.cstore.drive("A16-10")
    state = ds.load_state()
    state.update({"status": "recovering", "cycles_done": 5, "failure_cycles": [5],
                  "pending_recovery": {"cycle": 5, "code": "F1", "phase": "verify", "detail": {}}})
    ds.save_state(state)
    _hang(ctrl, "recovery", 5, phase="check")
    st = ds.load_state()
    assert st["status"] == "attention" and st["cycles_done"] == 5 and st["failure_cycles"] == [5]
    assert st["end_of_life"]["cycle"] == 5 and st["end_of_life"]["code"] == "F1"
    assert ds.read_cycles() == []


def test_a_hang_after_the_worker_booked_the_cycle_adds_nothing(ctrl):
    ds = ctrl.cstore.drive("A16-10")
    state = ds.load_state()
    state.update({"status": "recovering", "cycles_done": 6, "failure_cycles": [6],
                  "pending_recovery": {"cycle": 6, "code": "F1", "phase": "delete", "detail": {}}})
    ds.save_state(state)
    _hang(ctrl, "cycle", 6, phase="delete")  # the delete hung after the corruption was found
    st = ds.load_state()
    assert st["failure_cycles"] == [6] and st["status"] == "recovering" and st["pending_recovery"]


def test_only_one_enrollment_at_a_time(ctrl):
    ctrl.run = {"phase": "setup"}
    ctrl.submit("enroll", {"drive": "A16-05"})
    with pytest.raises(JobRefused):
        ctrl.submit("enroll", {"drive": "A16-12"})


# -- intake ---------------------------------------------------------------------------------------
def test_a_raw_stick_is_never_formatted(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, [])
    serial = by_hint["A8-06"]["serial"]
    plat.update_device(serial, raw=True)
    cand = next(c for c in candidates(sim_campaign, cs, plat) if c["serial"] == serial)
    assert cand["fs"] == "" and cand["total"] == 0
    with pytest.raises(IntakeError, match="cannot read a file system"):
        enroll(sim_campaign, cs, plat, "A8-06", guid=cand["guid"], serial=serial)
    assert os.listdir(os.path.join(plat.root, "volumes", serial)) == ["README.txt"]  # untouched


def test_the_format_result_must_come_from_the_same_stick(tmp_path, sim_campaign):
    plat, cs, by_hint = _rig(tmp_path, sim_campaign, [])
    serial = by_hint["A8-06"]["serial"]
    cand = next(c for c in candidates(sim_campaign, cs, plat) if c["serial"] == serial)
    real_format = plat.format_volume

    def format_then_swap(vol, label):
        result = real_format(vol, label)
        plat.storage_info = lambda root: StorageInfo(bus="USB", serial="ANOTHER-STICK")
        return result

    plat.format_volume = format_then_swap
    with pytest.raises(IntakeError, match="different stick"):
        enroll(sim_campaign, cs, plat, "A8-06", guid=cand["guid"], serial=serial)
    assert cs.drive("A8-06").load_intake() is None


# -- post-test ------------------------------------------------------------------------------------
def test_the_retention_check_reads_the_cycle_cut_at_the_stop(tmp_path):
    cs = CampaignStore(tmp_path, "t")
    ds = cs.drive("A8-06")
    for n in range(1, 6):
        ds.append_cycle({"drive": "A8-06", "cycle": n, "outcome": "pass"})
    ds.event("interrupted", 4, at_stop_time=False, free_before=100, workload="small")  # a pause, long done
    state = {"status": "censored"}
    assert _leftover_cycle(cs, "A8-06", state) is None
    ds.event("interrupted", 6, at_stop_time=True, free_before=200, workload="large", files_written=9)
    info = _leftover_cycle(cs, "A8-06", state)
    assert info["cycle"] == 6 and info["files_written"] == 9
