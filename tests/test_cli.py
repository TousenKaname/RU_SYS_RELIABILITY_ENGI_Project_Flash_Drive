"""End-to-end: self-test, rehearsal simulation, analysis, Excel export, enrollment."""

from __future__ import annotations

import pandas as pd

from flashrel.cli import main

from conftest import REPO


def test_selftest_passes(tmp_path):
    assert main(["selftest", "--dir", str(tmp_path / "st")]) == 0


def test_simulate_then_analyze(tmp_path):
    config = str(REPO / "configs" / "campaign.yaml")
    data = tmp_path / "data"
    assert main(["simulate", config, "--out", str(data), "--days", "12", "--seed", "3"]) == 0

    # Point a copy of the campaign at the simulated data folder.
    text = (REPO / "configs" / "campaign.yaml").read_text(encoding="utf-8")
    text = text.replace("data_dir: ../data", f"data_dir: {data.as_posix()}")
    cfg = tmp_path / "campaign.yaml"
    cfg.write_text(text, encoding="utf-8")
    (tmp_path / "inventory.yaml").write_text(
        (REPO / "configs" / "inventory.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    out = tmp_path / "report"
    assert main(["analyze", str(cfg), "--out", str(out)]) == 0
    life = pd.read_csv(out / "life_table.csv")
    assert len(life) == 9 and set(life["group"]) == {"S8", "A8", "A16"}
    assert (out / "fig_life.pdf").exists() and (out / "fall2026.xlsx").exists()
    sheets = pd.ExcelFile(out / "fall2026.xlsx").sheet_names
    assert {"Drives", "Cycles", "Events", "LifeTable", "ByGroup", "ByWorkload"} <= set(sheets)
    assert main(["status", str(cfg)]) == 0


def test_enroll_writes_identity_without_a_capacity_pass(campaign, drive, capsys):
    from flashrel.system.volumes import read_identity

    assert main(["enroll", str(campaign.source), "--drive", "T8-01", "--mount", str(drive)]) == 0
    assert read_identity(drive)["drive_id"] == "T8-01"
    assert "capacity_test" not in capsys.readouterr().out  # the first cycle does that check


def test_recheck_refuses_a_drive_that_is_not_in_attention(campaign, capsys):
    rc = main(["recheck", str(campaign.source), "--drive", "T8-01", "--wait", "0"])
    assert rc == 2 and "only for drives in the attention state" in capsys.readouterr().err


def test_portcheck_refuses_a_test_drive(campaign, drive, capsys):
    from flashrel.system.volumes import write_identity

    write_identity(drive, "T8-01")
    rc = main(["portcheck", str(campaign.source), "--port", "P1", "--mount", str(drive)])
    assert rc == 2 and "reference drive only" in capsys.readouterr().err
