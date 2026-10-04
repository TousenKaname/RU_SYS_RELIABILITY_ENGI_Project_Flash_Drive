"""Statistics checked against hand calculations and large simulated samples."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from flashrel.analysis.degradation import fit_paths
from flashrel.analysis.nonparametric import kaplan_meier, mean_cumulative_function, median_ranks
from flashrel.analysis.parametric import (
    fit_lognormal_mle,
    fit_weibull_mle,
    fit_weibull_rank_regression,
    lr_test_common_shape,
    weibayes_lower_bound,
)
from flashrel.analysis.planning import (
    cycle_hours,
    expected_failures,
    prob_at_least,
    units_for_failures,
)
from flashrel.analysis.regression import design_matrix, fit_weibull_regression

rng = np.random.default_rng(2026)


def censored_weibull(n, shape, scale, tau):
    t = scale * rng.weibull(shape, n)
    return np.minimum(t, tau), t <= tau


def test_kaplan_meier_hand_example():
    km = kaplan_meier([1, 2, 3, 4], [True, False, True, True])
    assert km["reliability"].round(4).tolist() == [0.75, 0.375, 0.0]
    assert km["at_risk"].tolist() == [4, 2, 1]


def test_median_ranks_bernard_and_johnson():
    complete = median_ranks([5, 1, 3], [True, True, True])
    assert complete["F"].round(4).tolist() == [round((i - 0.3) / 3.4, 4) for i in (1, 2, 3)]
    # 10F, 20S, 30F, 40F, 50S: adjusted ranks 1, 2.25, 3.5 (Johnson)
    johnson = median_ranks([10, 20, 30, 40, 50], [True, False, True, True, False])
    assert johnson["adjusted_rank"].tolist() == pytest.approx([1.0, 2.25, 3.5])


def test_weibull_mle_recovers_parameters_with_censoring():
    t, d = censored_weibull(4000, 2.5, 800.0, tau=900.0)
    fit = fit_weibull_mle(t, d)
    assert fit.shape == pytest.approx(2.5, rel=0.06)
    assert fit.scale == pytest.approx(800.0, rel=0.03)
    lo, hi = fit.interval("shape", 0.99)
    assert lo < 2.5 < hi
    assert fit.mttf == pytest.approx(800 * 0.887264, rel=0.04)  # Gamma(1.4) = 0.887264
    assert fit.mrl(0.0) == pytest.approx(fit.mttf, rel=1e-6)


def test_rank_regression_close_to_mle_on_complete_data():
    t = 500.0 * rng.weibull(3.0, 400)
    rr = fit_weibull_rank_regression(t, np.ones_like(t, bool))
    assert rr.shape == pytest.approx(3.0, rel=0.1)
    assert rr.scale == pytest.approx(500.0, rel=0.05)


def test_weibayes_zero_failure_bound():
    # n units all surviving tau with shape b: theta_L = tau * (n / (-ln(1 - C)))^(1/b)
    bound = weibayes_lower_bound([100.0] * 10, [False] * 10, shape=2.0, level=0.90)
    assert bound == pytest.approx(100.0 * (10 / np.log(10)) ** 0.5, rel=1e-6)
    with pytest.raises(ValueError):
        fit_weibull_mle([100.0] * 10, [False] * 10)


def test_weibayes_bounds_cover_exactly_the_groups_without_a_fit():
    from flashrel.analysis.report import try_weibull, weibayes_bounds

    worn = 300.0 * rng.weibull(2.0, 12)
    groups = {"A": (worn, np.ones(12, bool)), "B": (np.full(4, 290.0), np.zeros(4, bool))}
    fits = {g: f for g, (t, d) in groups.items() if (f := try_weibull(t, d)) is not None}
    bounds = weibayes_bounds(groups, fits)
    assert set(fits) == {"A"} and set(bounds) == {"B"}
    shape, theta_lower = bounds["B"]
    assert theta_lower == pytest.approx(weibayes_lower_bound(*groups["B"], shape))
    assert theta_lower > 290.0  # four survivors of 290 cycles push the bound past 290


def test_lognormal_and_common_shape_test():
    y = rng.normal(6.0, 0.5, 600)
    ln = fit_lognormal_mle(np.exp(y), np.ones(600, bool))
    assert ln.mu == pytest.approx(6.0, abs=0.06) and ln.sigma == pytest.approx(0.5, rel=0.1)
    groups = {g: censored_weibull(300, 2.0, s, tau=1500.0) for g, s in (("a", 600), ("b", 900))}
    test = lr_test_common_shape(groups)
    assert test["df"] == 1 and test["p_value"] > 0.001
    assert test["common_shape"] == pytest.approx(2.0, rel=0.12)


def test_weibull_regression_recovers_life_ratio():
    n = 1500
    frame = pd.DataFrame({"brand": rng.choice(["ABLAZE", "SanDisk"], n),
                          "capacity_gb": rng.choice([8, 16], n)})
    x, names = design_matrix(frame, ["brand", "capacity_gb"])
    beta = np.array([np.log(600.0), np.log(2.0), np.log(0.8)])
    sigma = 0.5
    w = np.log(rng.exponential(size=n))  # standard smallest-extreme-value
    t = np.exp(x @ beta + sigma * w)
    tau = np.quantile(t, 0.8)
    model = fit_weibull_regression(np.minimum(t, tau), t <= tau, x, names)
    assert names == ("intercept", "brand[SanDisk]", "capacity_gb[16]")  # 8 GB is the baseline
    table = model.table()
    ratio = table.set_index("term")["life_ratio"]
    assert ratio["brand[SanDisk]"] == pytest.approx(2.0, rel=0.1)
    assert ratio["capacity_gb[16]"] == pytest.approx(0.8, rel=0.1)
    assert model.shape == pytest.approx(2.0, rel=0.1)


def test_factor_levels_are_natural_and_readable():
    from flashrel.analysis.regression import factor_labels

    labels, levels = factor_labels(pd.Series([16.0, 8.0, 16.0]))
    assert levels == ["8", "16"] and labels.tolist() == ["16", "8", "16"]


def test_mcf_counts_events_per_unit_at_risk():
    mcf = mean_cumulative_function([([2, 5], 10), ([5], 6), ([], 3)])
    assert mcf["time"].tolist() == [2, 5]
    assert mcf["mcf"].tolist() == pytest.approx([1 / 3, 1 / 3 + 2 / 2])


def test_degradation_pseudo_failure():
    cycles = np.arange(1, 61)
    rel = np.exp(-0.01 * cycles)
    paths = pd.DataFrame({"drive_id": "X", "workload": "large", "cycle": cycles,
                          "relative": rel})
    out = fit_paths(paths, threshold=0.5).iloc[0]
    assert out["pseudo_failure"] and out["pseudo_time"] == pytest.approx(np.log(2) / 0.01,
                                                                         rel=1e-6)


def test_planning_arithmetic():
    hours = cycle_hours(8, write_mbps=6.0, read_mbps=20.0, fill_fraction=0.9, overhead_s=0,
                        usable_fraction=1.0)
    assert hours == pytest.approx(7.2e9 / 6e6 / 3600 + 7.2e9 / 20e6 / 3600)
    assert expected_failures(10, 800.0, 2.0, 800.0) == pytest.approx(10 * (1 - np.exp(-1)))
    assert prob_at_least(1, 3, 0.5) == pytest.approx(0.875)
    n = units_for_failures(20, 0.5)
    assert prob_at_least(20, n, 0.5) >= 0.9 > prob_at_least(20, n - 1, 0.5)
