"""End-to-end cycles on a temporary folder, with injected faults."""

from __future__ import annotations

import errno
import threading
from pathlib import Path

from helpers import capped_planner

from flashrel.cycle import CycleRunner, Outcome, run_cycle
from flashrel.payload import PAYLOAD_DIR
from flashrel.verify import CorruptionKind, FileStatus


def _run(campaign, drive, workload="small", fault_hook=None, **kw):
    w = campaign.workloads[workload]
    return run_cycle(drive, capped_planner(campaign, w), campaign.cycle, drive_id="T8-01",
                     cycle=1, workload=w.name, fault_hook=fault_hook, **kw)


def test_clean_cycle_passes_and_cleans_up(campaign, drive):
    result = _run(campaign, drive)
    assert result.outcome is Outcome.PASS
    assert result.write.bytes == result.planned_bytes == result.verify.bytes
    assert result.write.files == result.planned_files > 10
    assert result.write.mbps > 0 and result.verify.mbps > 0
    assert list((drive / PAYLOAD_DIR).iterdir()) == []


def test_flipped_bit_is_corruption(campaign, drive):
    def flip(cycle_dir: Path, plan):
        target = cycle_dir / plan.files[2].relpath
        raw = bytearray(target.read_bytes())
        raw[len(raw) // 2] ^= 0x10
        target.write_bytes(bytes(raw))

    result = _run(campaign, drive, fault_hook=flip)
    assert result.outcome is Outcome.CORRUPTION
    assert result.files_corrupt == 1 and result.bits_corrupt == 1
    assert result.corruption_kinds == {CorruptionKind.BIT_ERRORS.value: 1}


def test_missing_and_half_files(campaign, drive):
    def damage(cycle_dir: Path, plan):
        (cycle_dir / plan.files[0].relpath).unlink()
        half = cycle_dir / plan.files[1].relpath
        half.write_bytes(half.read_bytes()[: plan.files[1].size // 2])

    result = _run(campaign, drive, workload="large", fault_hook=damage)
    statuses = sorted(c.status.value for c in result.bad_files)
    assert result.outcome is Outcome.CORRUPTION
    assert statuses == [FileStatus.CORRUPT.value, FileStatus.MISSING.value]
    assert "truncated" in result.corruption_kinds


def test_stop_event_aborts_without_counting(campaign, drive):
    stop = threading.Event()
    stop.set()
    assert _run(campaign, drive, stop=stop).outcome is Outcome.ABORTED


def test_transient_write_error_is_retried(campaign, drive, monkeypatch):
    import flashrel.cycle as cycle_module

    real_write, calls = cycle_module.write_file, {"n": 0}

    def flaky(path, chunks, sync):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(errno.EIO, "simulated I/O error", str(path))
        return real_write(path, chunks, sync=sync)

    monkeypatch.setattr(cycle_module, "write_file", flaky)
    result = _run(campaign, drive)
    assert result.outcome is Outcome.PASS
    assert result.transient_errors == 1
    assert [e.recovered for e in result.errors] == [True]


def test_persistent_write_error_ends_cycle(campaign, drive, monkeypatch):
    import flashrel.cycle as cycle_module

    def broken(path, chunks, sync):
        raise OSError(errno.EIO, "simulated I/O error", str(path))

    monkeypatch.setattr(cycle_module, "write_file", broken)
    result = _run(campaign, drive)
    assert result.outcome is Outcome.IO_ERROR
    assert result.errors[-1].attempts == campaign.cycle.retries + 1


def test_vanished_drive_is_disconnect(campaign, drive):
    def unplug(cycle_dir: Path, plan):
        import shutil

        shutil.rmtree(drive)

    result = _run(campaign, drive, fault_hook=unplug)
    assert result.outcome is Outcome.DISCONNECTED


def test_leftovers_of_an_interrupted_cycle_are_removed(campaign, drive):
    stale = drive / PAYLOAD_DIR / "c000007" / "d0000"
    stale.mkdir(parents=True)
    (stale / "f000000.bin").write_bytes(b"x" * 100)
    w = campaign.workloads["small"]
    runner = CycleRunner(drive, capped_planner(campaign, w), campaign.cycle, drive_id="T8-01",
                         cycle=1, workload="small")
    result = runner.run()
    assert result.outcome is Outcome.PASS and result.leftover_dirs == 1
