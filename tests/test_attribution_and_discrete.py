from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from flashrel.analysis.attribution import flag_common_cause, hub_of
from flashrel.analysis.discrete import fit_cloglog, per_cycle_design


def test_hub_labels():
    assert hub_of("A2") == "A" and hub_of("W-direct-1") == "W-direct" and hub_of("B") == "B"


def test_common_cause_bursts():
    events = pd.DataFrame({
        "drive_id": ["A8-02", "A8-03", "A8-04", "S8-02"],
        "time": ["2026-10-20T10:00:00Z", "2026-10-20T10:04:00Z", "2026-10-22T08:00:00Z",
                 "2026-10-20T10:02:00Z"],
        "host": ["W", "W", "W", "M"],
        "port": ["A1", "A2", "A3", "B1"],
    })
    flagged = flag_common_cause(events).set_index("drive_id")["common_cause"]
    assert flagged.to_dict() == {"A8-02": True, "A8-03": True, "A8-04": False, "S8-02": False}


def test_cloglog_recovers_workload_hazard_ratio():
    rng = np.random.default_rng(5)
    rows = []
    for drive in range(40):
        for k in range(1, 301):
            workload = ("small", "medium", "large")[(k - 1) % 3]
            eta = -6.0 + 0.6 * np.log(k) + (np.log(3.0) if workload == "small" else 0.0)
            failed = rng.random() < 1 - np.exp(-np.exp(eta))
            rows.append({"drive_id": drive, "cycle": k, "workload": workload,
                         "outcome": "corruption" if failed else "pass"})
    x, y, names, clusters = per_cycle_design(pd.DataFrame(rows))
    fit = fit_cloglog(x, y, names, clusters=clusters)
    table = fit.table().set_index("term")
    assert fit.robust and set(table["se_type"]) == {"cluster-robust"}
    assert table.loc["workload[small]", "hazard_ratio"] == pytest.approx(3.0, rel=0.35)
    assert table.loc["workload[large]", "hazard_ratio"] == pytest.approx(1.0, abs=0.45)


def test_missing_reference_workload_falls_back():
    rows = [{"drive_id": d, "cycle": k, "workload": ("small", "large")[k % 2],
             "outcome": "corruption" if (d + k) % 37 == 0 else "pass"}
            for d in range(6) for k in range(1, 200)]
    x, y, names, clusters = per_cycle_design(pd.DataFrame(rows), reference_workload="medium")
    assert np.linalg.matrix_rank(x) == x.shape[1]
    assert [n for n in names if n.startswith("workload")] == ["workload[small]"]


def test_port_faults_are_not_trials():
    rows = [{"drive_id": 1, "cycle": k, "workload": "medium",
             "outcome": "port_fault" if k == 5 else "pass"} for k in range(1, 11)]
    x, y, names, clusters = per_cycle_design(pd.DataFrame(rows))
    assert len(y) == 9 and y.sum() == 0
