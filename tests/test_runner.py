"""The per-drive worker loop: rotation, censoring, recovery, intermittent limit, hangs."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path

import pytest
from helpers import capped_planner

import flashrel.runner as runner
from flashrel.config import StopRule
from flashrel.failure import DriveStatus, EventType
from flashrel.recorder import DriveLog
from flashrel.units import MiB


@pytest.fixture
def worker_env(campaign, drive, monkeypatch):
    """A campaign stopped after six cycles, with the drive mounted at a temp folder."""
    camp = dataclasses.replace(campaign, stop=StopRule(max_cycles=6))
    monkeypatch.setattr(runner, "wait_for_drive", lambda *args, **kwargs: drive)
    monkeypatch.setattr(runner, "make_planner",
                        lambda c, d, cyc, w, budget_cap=None, key=None:
                        capped_planner(c, w, cycle=cyc, budget=1 * MiB, drive_id=key or d))
    return camp


def corrupt_on(cycles: set[int], monkeypatch) -> None:
    """Flip one bit after the write phase of the listed cycles."""
    real = runner.run_cycle

    def flip(cycle_dir: Path, plan):
        target = cycle_dir / plan.files[0].relpath
        raw = bytearray(target.read_bytes())
        raw[-1] ^= 0x01
        target.write_bytes(bytes(raw))

    def wrapped(*args, **kwargs):
        if kwargs.get("cycle") in cycles:
            kwargs["fault_hook"] = flip
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "run_cycle", wrapped)


def events(camp) -> list:
    return DriveLog(camp.run_dir, "T8-01").read_events()


def test_worker_rotates_workloads_until_censored(worker_env):
    status = runner.DriveWorker(worker_env, "T8-01", "H", threading.Event()).run()
    rows = DriveLog(worker_env.run_dir, "T8-01").read_cycles()
    assert status is DriveStatus.CENSORED
    assert [r["workload"] for r in rows] == ["small", "large"] * 3
    assert {r["outcome"] for r in rows} == {"pass"}
    assert events(worker_env)[-1].type is EventType.CENSORED


def test_soft_failure_recovers_and_testing_continues(worker_env, monkeypatch):
    corrupt_on({2}, monkeypatch)
    status = runner.DriveWorker(worker_env, "T8-01", "H", threading.Event()).run()
    kinds = [(e.type, e.code, e.cycle) for e in events(worker_env)]
    assert status is DriveStatus.CENSORED
    assert (EventType.CYCLE_FAILURE, "F1", 2) in kinds
    assert (EventType.RECOVERED, "D2", 2) in kinds
    assert DriveLog(worker_env.run_dir, "T8-01").load_state().cycles_done == 6


def test_intermittent_limit_ends_life(worker_env, monkeypatch):
    corrupt_on({2, 3}, monkeypatch)  # fixture policy: 2 failures within 5 cycles
    status = runner.DriveWorker(worker_env, "T8-01", "H", threading.Event()).run()
    state = DriveLog(worker_env.run_dir, "T8-01").load_state()
    hard = [e for e in events(worker_env) if e.type is EventType.HARD_FAILURE]
    assert status is DriveStatus.FAILED and state.cycles_done == 3
    assert [(e.code, e.cycle) for e in hard] == [("F1", 3)]


def test_hang_is_a_failed_cycle_for_the_operator(campaign):
    status = runner.record_hang(campaign, "T8-01", host="H", idle_s=700)
    log = DriveLog(campaign.run_dir, "T8-01")
    state = log.load_state()
    kinds = [(e.type, e.code) for e in events(campaign)]
    assert status is DriveStatus.ATTENTION and state.cycles_done == 1
    assert state.soft_failures == [1]
    assert kinds == [(EventType.CYCLE_FAILURE, "F3"), (EventType.ATTENTION, "F3")]
    rows = log.read_cycles()
    assert [(r["cycle"], r["outcome"], r["host"], r["port"]) for r in rows] == \
        [("1", "hang", "H", "P1")]
    failure = events(campaign)[0]
    assert failure.detail["host"] == "H" and failure.detail["port"] == "P1"


def test_repeated_hangs_reach_the_intermittent_limit(campaign):
    runner.record_hang(campaign, "T8-01", host="H", idle_s=700)
    status = runner.record_hang(campaign, "T8-01", host="H", idle_s=700)
    assert status is DriveStatus.FAILED  # fixture policy: 2 failures within 5 cycles
    assert [e.type for e in events(campaign)][-1] is EventType.HARD_FAILURE


def test_restart_never_counts_a_logged_cycle_twice(worker_env, monkeypatch):
    log = DriveLog(worker_env.run_dir, "T8-01")
    runner.DriveWorker(dataclasses.replace(worker_env, stop=StopRule(max_cycles=2)), "T8-01",
                       "H", threading.Event()).run()
    # Simulate a worker that died after logging cycle 3 but before saving its state.
    state = log.load_state()
    state.status = DriveStatus.ACTIVE
    log.save_state(state)
    rows = log.read_cycles()
    with open(log.cycles_path, "a", encoding="utf-8", newline="") as fh:
        fh.write(",".join(rows[-1].values()).replace(",2,", ",3,", 1) + "\r\n")
    status = runner.DriveWorker(worker_env, "T8-01", "H", threading.Event()).run()
    cycles = [int(r["cycle"]) for r in log.read_cycles()]
    assert status is DriveStatus.CENSORED and cycles == [1, 2, 3, 4, 5, 6]


def test_missing_drive_is_an_f3_cycle_failure(campaign, monkeypatch):
    monkeypatch.setattr(runner, "wait_for_drive", lambda *args, **kwargs: None)
    status = runner.DriveWorker(campaign, "T8-01", "H", threading.Event()).run()
    rows = DriveLog(campaign.run_dir, "T8-01").read_cycles()
    assert status is DriveStatus.ATTENTION
    assert [(r["cycle"], r["outcome"]) for r in rows] == [("1", "disconnected")]
    assert [(e.type, e.code) for e in events(campaign)][0] == (EventType.CYCLE_FAILURE, "F3")


def test_worker_skips_a_drive_moved_to_another_host(worker_env):
    log = DriveLog(worker_env.run_dir, "T8-01")
    state = log.load_state()
    state.host = "W"
    log.save_state(state)
    status = runner.DriveWorker(worker_env, "T8-01", "M", threading.Event()).run()
    assert status is DriveStatus.ACTIVE and log.read_cycles() == []


def test_recovery_checks_use_fresh_data(campaign, drive):
    first = runner.make_planner(campaign, "T8-01", 0, runner.CHECK_WORKLOAD, 1 * MiB, key="a")
    second = runner.make_planner(campaign, "T8-01", 0, runner.CHECK_WORKLOAD, 1 * MiB, key="b")
    from flashrel.system.volumes import VolumeUsage

    volume = VolumeUsage(total=10**9, free=10**9, cluster_size=4096)
    assert first(volume).files[0].key != second(volume).files[0].key
