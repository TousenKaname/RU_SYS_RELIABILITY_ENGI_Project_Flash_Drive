"""Cycle engine, sentinel and failure rules on real files in a temporary folder."""

from __future__ import annotations

import base64
import os
import threading

from flashrel.config import FailurePolicy, SentinelSpec, Workload
from flashrel.cycle import CycleRunner, Hooks
from flashrel.failure import DriveState, Outcome, limit_reached, track_throughput
from flashrel.payload import fill_budget, identity_bytes, plan_cycle, sentinel_bytes
from flashrel.platform.base import Platform, Volume
from flashrel.sentinel import IDENTITY_FILE, SENTINEL_FILE, SentinelChecker
from flashrel.store import FileLock, append_csv, read_csv

W = Workload("medium", 8 * 1024, 300 * 1024, 16)


class TmpPlatform(Platform):
    """Plain files; a fixed 'capacity' so the planner has something to fill."""

    def usage(self, root):
        used = sum(os.path.getsize(os.path.join(b, f)) for b, _, fs in os.walk(root) for f in fs)
        return 40 * 2**20, 40 * 2**20 - used, 4096


def _runner(tmp_path, cycle=1, **hooks):
    def planner(free, cluster):
        return plan_cycle(seed=1, drive_id="T-01", cycle=cycle, workload=W,
                          budget_bytes=fill_budget(free, 0.5, 2**20), cluster_size=cluster, chunk_size=2**20)
    from flashrel.config import CycleSpec

    return CycleRunner(TmpPlatform(), str(tmp_path), planner, CycleSpec(retry_backoff_s=0.0),
                       drive_id="T-01", cycle=cycle, workload="medium", hooks=Hooks(**hooks), sleep=lambda s: None)


def test_clean_cycle_passes_and_counts_the_dose(tmp_path):
    r = _runner(tmp_path).run()
    assert r.outcome is Outcome.PASS
    assert r.dose.files_created == r.dose.files_deleted == r.planned_files > 10
    assert r.dose.bytes_written == r.dose.bytes_read == r.planned_bytes
    assert not os.listdir(tmp_path / "FLASHREL-DATA")


def test_injected_flip_is_corruption(tmp_path):
    def flip(cycle_dir, plan):
        path = os.path.join(cycle_dir, *plan.files[2].relpath.split("/"))
        with open(path, "r+b") as fh:
            fh.seek(100)
            fh.write(b"\x00" if fh.read(1) != b"\x00" else b"\x01")

    r = _runner(tmp_path, after_write=flip).run()
    assert r.outcome is Outcome.CORRUPTION and r.files_corrupt == 1


def test_missing_file_is_corruption_and_delete_skips_it(tmp_path):
    def lose(cycle_dir, plan):
        os.remove(os.path.join(cycle_dir, *plan.files[0].relpath.split("/")))

    r = _runner(tmp_path, after_write=lose).run()
    assert r.outcome is Outcome.CORRUPTION and r.files_missing == 1


def test_stop_ends_cycle_in_the_delete_phase(tmp_path):
    """Run 1's gap: the stop time was not checked during the delete phase."""
    seen = {"delete": 0}

    def should_stop():
        return seen["delete"] > 3

    def progress(phase, files, nbytes):
        if phase == "delete":
            seen["delete"] += files

    r = _runner(tmp_path, should_stop=should_stop, progress=progress).run()
    assert r.outcome is Outcome.ABORTED and r.phase == "delete"
    assert 0 < r.dose.files_deleted < r.planned_files


def test_leftovers_are_cleared_but_quarantine_is_kept(tmp_path):
    (tmp_path / "FLASHREL-DATA" / "c000007" / "d0000").mkdir(parents=True)
    (tmp_path / "FLASHREL-DATA" / "c000007" / "d0000" / "x.bin").write_bytes(b"x")
    (tmp_path / "FLASHREL-DATA" / "c000003").mkdir()
    runner = _runner(tmp_path, cycle=8)
    runner.quarantined = {"FLASHREL-DATA/c000003"}
    r = runner.run()
    assert r.outcome is Outcome.PASS and r.leftover_dirs == 1
    assert sorted(os.listdir(tmp_path / "FLASHREL-DATA")) == ["c000003"]


def _intake(tmp_path):
    ident = identity_bytes({"drive_id": "T-01", "token": "tok"})
    (tmp_path / IDENTITY_FILE).write_bytes(ident)
    (tmp_path / SENTINEL_FILE).write_bytes(sentinel_bytes("T-01", "tok", 65536))
    return {"drive_id": "T-01", "token": "tok", "identity_b64": base64.b64encode(ident).decode(),
            "sentinel_size": 65536, "exfat": {}}


def test_sentinel_confirms_damage_at_rest(tmp_path):
    intake = _intake(tmp_path)
    vol = Volume(root=str(tmp_path) + os.sep, fs="exFAT")
    checker = SentinelChecker(Platform(), intake, SentinelSpec(confirm_reads=3, confirm_gap_s=0), sleep=lambda s: None)
    assert checker.check(vol).ok
    data = bytearray((tmp_path / SENTINEL_FILE).read_bytes())
    data[1000] ^= 0xFF
    (tmp_path / SENTINEL_FILE).write_bytes(bytes(data))
    res = checker.check(vol)
    assert not res.ok and res.failed == "sentinel" and res.code == "F1"
    assert res.components["sentinel"]["wrong_bits"] == 8 and res.reads == 3
    # damage on record is not counted again
    assert checker.check(vol, skip={"sentinel"}).ok


def test_sentinel_ignores_an_anomaly_that_clears(tmp_path):
    intake = _intake(tmp_path)
    good = (tmp_path / SENTINEL_FILE).read_bytes()
    bad = bytearray(good)
    bad[5] ^= 1
    (tmp_path / SENTINEL_FILE).write_bytes(bytes(bad))

    def heal(seconds):  # the second read sees the right data again (a torn read, say)
        (tmp_path / SENTINEL_FILE).write_bytes(good)

    checker = SentinelChecker(Platform(), intake, SentinelSpec(confirm_reads=3, confirm_gap_s=0), sleep=heal)
    res = checker.check(Volume(root=str(tmp_path) + os.sep, fs="exFAT"))
    assert res.ok and res.warnings


def test_failure_rules():
    policy = FailurePolicy()
    st = DriveState("T-01")
    for mbps in (10.0, 10.4):
        assert track_throughput(st, "large", mbps, policy) is None
    assert track_throughput(st, "large", 4.0, policy) is None
    assert track_throughput(st, "large", 4.5, policy) is None
    slow = track_throughput(st, "large", 4.2, policy)
    assert slow and slow["workload"] == "large"
    st.cycles_done, st.failure_cycles = 12, [2, 9, 11]
    assert not limit_reached(st, policy)  # cycle 2 is outside the last 10 (3..12)
    st.failure_cycles.append(12)
    assert limit_reached(st, policy)


def test_store_survives_a_torn_line_and_locks(tmp_path):
    path = tmp_path / "c.csv"
    append_csv(path, ("a", "b"), {"a": 1, "b": 2})
    with open(path, "a") as fh:
        fh.write("3,")  # power cut in the middle of a row
    append_csv(path, ("a", "b"), {"a": 5, "b": 6})
    assert [r["a"] for r in read_csv(path)] == ["1", "5"]
    assert (tmp_path / "c.csv.torn").read_text().strip() == "3,"  # kept aside, not lost
    lock = FileLock(tmp_path / ".lock")
    assert lock.acquire()
    other = FileLock(tmp_path / ".lock")
    result = []
    t = threading.Thread(target=lambda: result.append(other.acquire()))
    t.start()
    t.join()
    assert result == [False]
    lock.release()
    assert other.acquire()
    other.release()
