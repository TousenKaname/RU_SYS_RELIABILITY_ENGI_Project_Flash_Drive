from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flashrel.config import ConfigError, FailurePolicy, load_campaign
from flashrel.cycle import CycleResult, ErrorRecord, Outcome, PhaseStats
from flashrel.failure import (
    DriveState,
    DriveStatus,
    Event,
    EventType,
    apply_cycle,
    intermittent_limit_reached,
)
from flashrel.recorder import DriveBusy, DriveLog, drive_lock
from flashrel.system.errors import ErrorKind

from conftest import REPO

POLICY = FailurePolicy(baseline_cycles=2, slow_fraction=0.5, slow_window=2,
                       intermittent_limit=2, intermittent_window=5)


def result(cycle: int, mbps: float = 10.0, outcome: Outcome = Outcome.PASS,
           recovered_errors: int = 0) -> CycleResult:
    r = CycleResult(drive_id="T8-01", cycle=cycle, workload="large", host="H", port="P1",
                    started_at=datetime.now(timezone.utc))
    r.write = PhaseStats(files=1, bytes=int(mbps * 1e6), seconds=1.0)
    r.outcome = outcome
    r.errors = [ErrorRecord("write", ErrorKind.IO_ERROR, "f", "EIO", 2, True)
                for _ in range(recovered_errors)]
    return r


def test_baseline_then_slowdown_flagged_once():
    state = DriveState("T8-01")
    events = []
    for c, mbps in enumerate([10, 10, 9, 4, 4, 3, 3], start=1):
        events += apply_cycle(state, result(c, mbps), POLICY)
    slow = [e for e in events if e.type is EventType.SLOW]
    assert state.baseline_mbps["large"] == 10
    assert [e.cycle for e in slow] == [5]
    assert slow[0].code == "D1"


def test_transient_errors_and_cycle_failures():
    state = DriveState("T8-01")
    events = apply_cycle(state, result(1, recovered_errors=2), POLICY)
    assert [(e.type, e.code, e.detail["count"]) for e in events] == [(EventType.TRANSIENT, "D3", 2)]
    events = apply_cycle(state, result(2, outcome=Outcome.CORRUPTION), POLICY)
    assert [(e.type, e.code) for e in events] == [(EventType.CYCLE_FAILURE, "F1")]
    assert state.soft_failures == [2] and not intermittent_limit_reached(state, POLICY)
    apply_cycle(state, result(4, outcome=Outcome.IO_ERROR), POLICY)
    assert intermittent_limit_reached(state, POLICY)


def test_aborted_cycle_changes_nothing():
    state = DriveState("T8-01")
    assert apply_cycle(state, result(1, outcome=Outcome.ABORTED), POLICY) == []
    assert state.cycles_done == 0


def test_drive_log_round_trip(tmp_path):
    log = DriveLog(tmp_path, "T8-01")
    state = DriveState("T8-01", status=DriveStatus.ATTENTION, cycles_done=7, soft_failures=[3])
    log.save_state(state)
    assert log.load_state().status is DriveStatus.ATTENTION
    assert log.load_state().soft_failures == [3]
    log.append_cycle(result(1))
    log.append_cycle(result(2, outcome=Outcome.CORRUPTION))
    rows = log.read_cycles()
    assert [r["outcome"] for r in rows] == ["pass", "corruption"]
    log.append_event(Event("T8-01", 2, EventType.CYCLE_FAILURE, "F1", {"x": 1}))
    assert log.read_events()[0].detail == {"x": 1}


def test_drive_lock_is_exclusive():
    with drive_lock("unit-test", "T8-99"):
        with pytest.raises(DriveBusy):
            with drive_lock("unit-test", "T8-99"):
                pass
    with drive_lock("unit-test", "T8-99"):
        pass


def test_repository_configs_load():
    campaign = load_campaign(REPO / "configs" / "campaign.yaml")
    assert len(campaign.assignments) == 9
    assert {a.host for a in campaign.assignments.values()} == {"W"}  # Windows desktop only
    paths: dict[str, set[str]] = {}  # connection path (W rear panel, A/B docks) -> groups
    spans: dict[str, set[str]] = {}  # group -> connection paths
    for a in campaign.assignments.values():
        group = campaign.inventory[a.drive_id].group.code
        paths.setdefault(a.port[0], set()).add(group)
        spans.setdefault(group, set()).add(a.port[0])
    assert set(paths) == {"W", "A", "B"}
    assert all(len(groups) >= 2 for groups in paths.values())
    assert all(len(p) >= 2 for p in spans.values())
    assert [campaign.workload_for_cycle(c).name for c in (1, 2, 3, 4)] == \
        ["small", "medium", "large", "small"]
    # the test ends on 18 Oct 2026, 09:00, in time for the analysis and the slides
    assert campaign.stop.end_time == datetime(2026, 10, 18, 9, 0)


def test_bad_config_is_rejected(tmp_path):
    (tmp_path / "inventory.yaml").write_text((REPO / "configs" / "inventory.yaml").read_text())
    (tmp_path / "c.yaml").write_text(
        "campaign: x\nseed: 1\nworkloads: {w: {min_size: 4KiB, max_size: 1KiB}}\n"
        "assignments: {S8-01: {host: W, port: W1}}\n")
    with pytest.raises(ConfigError):
        load_campaign(tmp_path / "c.yaml")
