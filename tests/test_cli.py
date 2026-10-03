"""End-to-end: self-test, rehearsal simulation, analysis and Excel export."""

from __future__ import annotations

import pandas as pd

from flashrel.cli import main

from conftest import REPO


def test_selftest_passes(tmp_path):
    assert main(["selftest", "--dir", str(tmp_path / "st")]) == 0


def test_simulate_then_analyze(tmp_path):
    config = str(REPO / "configs" / "phase1.yaml")
    data = tmp_path / "data"
    assert main(["simulate", config, "--out", str(data), "--days", "50", "--seed", "3"]) == 0

    # Point a copy of the campaign at the simulated data folder.
    text = (REPO / "configs" / "phase1.yaml").read_text(encoding="utf-8")
    text = text.replace("data_dir: ../data", f"data_dir: {data.as_posix()}")
    cfg = tmp_path / "phase1.yaml"
    cfg.write_text(text, encoding="utf-8")
    (tmp_path / "inventory.yaml").write_text(
        (REPO / "configs" / "inventory.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    out = tmp_path / "report"
    assert main(["analyze", str(cfg), "--out", str(out)]) == 0
    life = pd.read_csv(out / "life_table.csv")
    assert len(life) == 9 and set(life["group"]) == {"S8", "A8", "A16"}
    assert (out / "fig_life.pdf").exists() and (out / "phase1.xlsx").exists()
    sheets = pd.ExcelFile(out / "phase1.xlsx").sheet_names
    assert {"Drives", "Cycles", "Events", "LifeTable", "ByGroup", "ByWorkload"} <= set(sheets)
    assert main(["status", str(cfg)]) == 0
