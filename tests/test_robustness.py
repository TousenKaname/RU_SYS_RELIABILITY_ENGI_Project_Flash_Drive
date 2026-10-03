"""Data-integrity guards: torn logs, port-fault re-attribution, endpoints, edge cases."""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import numpy as np
import pytest

from flashrel.analysis.lifedata import cycles_frame, life_table
from flashrel.analysis.nonparametric import mean_cumulative_function
from flashrel.analysis.report import fit_table
from flashrel.config import load_campaign
from flashrel.failure import Event, EventType
from flashrel.payload import FileSpec, iter_expected
from flashrel.recorder import DriveLog
from flashrel.system.directio import read_chunks, write_file

from test_failure_and_logs import result


def test_torn_lines_are_isolated_and_skipped(tmp_path):
    log = DriveLog(tmp_path, "T8-01")
    log.append_event(Event("T8-01", 1, EventType.NOTE, "", {"text": "first"}))
    with open(log.events_path, "a", encoding="utf-8") as fh:
        fh.write('{"drive_id": "T8-01", "cycle": 2, "ty')  # power cut mid-record
    log.append_event(Event("T8-01", 3, EventType.NOTE, "", {"text": "after"}))
    assert [e.detail["text"] for e in log.read_events()] == ["first", "after"]

    log.append_cycle(result(1))
    with open(log.cycles_path, "a", encoding="utf-8", newline="") as fh:
        fh.write("T8-01,2,large,H")  # torn row
    log.append_cycle(result(3))
    assert [r["cycle"] for r in log.read_cycles()] == ["1", "3"]


def test_port_fault_is_removed_from_every_failure_count(campaign):
    from flashrel.cycle import Outcome

    log = DriveLog(campaign.run_dir, "T8-01")
    for c, outcome in ((1, Outcome.PASS), (2, Outcome.CORRUPTION), (3, Outcome.IO_ERROR)):
        log.append_cycle(result(c, outcome=outcome))
        if outcome is not Outcome.PASS:
            log.append_event(Event("T8-01", c, EventType.CYCLE_FAILURE, "F1", {}))
    log.append_event(Event("T8-01", 2, EventType.PORT_FAULT, "F5", {"failure_cycle": 2}))
    assert log.failure_cycles() == [3]
    frame = cycles_frame(campaign)
    assert frame.set_index("cycle")["outcome"].to_dict() == \
        {1: "pass", 2: "port_fault", 3: "io_error"}


def test_hard_failure_without_earlier_failure_counts_as_first_failure(campaign):
    log = DriveLog(campaign.run_dir, "T8-01")
    for c in range(1, 41):
        log.append_cycle(result(c))
    log.append_event(Event("T8-01", 40, EventType.HARD_FAILURE, "F3", {}))
    row = life_table(campaign).set_index("drive_id").loc["T8-01"]
    assert (row["t_first"], row["first_failed"], row["t_hard"], row["hard_failed"]) == \
        (40, True, 40, True)


def test_mcf_ignores_events_after_a_unit_left_observation():
    mcf = mean_cumulative_function([([50], 49), ([], 100), ([], 100)])
    assert mcf["mcf"].tolist() == [0.0]


def test_tied_failures_fall_back_to_weibayes():
    table = fit_table({"S8": (np.array([300.0, 300.0]), np.array([True, True]))})
    assert np.isnan(table.loc[0].get("shape", np.nan))
    assert table.loc[0, "scale_lower_bound"] > 0


def test_aware_stop_time_is_compared_in_local_time(tmp_path):
    from conftest import REPO

    (tmp_path / "inventory.yaml").write_text((REPO / "configs" / "inventory.yaml").read_text())
    (tmp_path / "c.yaml").write_text(textwrap.dedent("""
        campaign: x
        seed: 1
        workloads: {w: {min_size: 4KiB, max_size: 8KiB}}
        stop: {end_time: 2026-12-01T14:00:00Z}
        assignments: {S8-01: {host: W, port: W1}}
    """))
    stop = load_campaign(tmp_path / "c.yaml").stop
    assert stop.end_time.tzinfo is None
    expected = datetime(2026, 12, 1, 14, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert stop.end_time == expected
    assert stop.reached(0, datetime(2030, 1, 1)) and not stop.reached(0, datetime(2020, 1, 1))


def test_periodic_flush_keeps_content_exact(tmp_path):
    spec = FileSpec(index=0, relpath="f.bin", size=5 * 65536 + 123, key=99)
    path = tmp_path / "f.bin"
    write_file(path, iter_expected(spec, 1, 65536), sync=True, sync_every=2 * 65536)
    assert b"".join(read_chunks(path, 65536)) == b"".join(iter_expected(spec, 1, 65536))


@pytest.mark.parametrize("bad_cycle", [7])
def test_port_fault_command_rejects_unknown_failures(campaign, bad_cycle, capsys):
    from flashrel.cli import main

    rc = main(["port-fault", str(campaign.source), "--drive", "T8-01", "--cycle",
               str(bad_cycle), "--port", "P1", "--reason", "test"])
    assert rc == 2 and "not a logged failure" in capsys.readouterr().err
