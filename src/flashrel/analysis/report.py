"""One call from campaign logs to tables, figures and the Excel workbook."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from flashrel.analysis.attribution import flag_common_cause
from flashrel.analysis.degradation import fit_paths, normalised_paths
from flashrel.analysis.discrete import fit_cloglog, per_cycle_design
from flashrel.analysis.export import write_workbook
from flashrel.analysis.lifedata import cycles_frame, events_frame, life_table, recurrent_units
from flashrel.analysis.nonparametric import mean_cumulative_function
from flashrel.analysis.parametric import (
    fit_lognormal_mle,
    fit_weibull_mle,
    lr_test_common_shape,
    weibayes_lower_bound,
)
from flashrel.analysis.regression import design_matrix, fit_weibull_regression
from flashrel.config import Campaign
from flashrel.viz import style
from flashrel.viz.plots import degradation_plot, km_plot, mcf_plot, weibull_probability_plot

ENDPOINTS = {"hard": ("t_hard", "hard_failed"), "first": ("t_first", "first_failed")}


def group_data(life: pd.DataFrame, endpoint: str = "hard") -> dict[str, tuple[np.ndarray,
                                                                              np.ndarray]]:
    time_col, flag_col = ENDPOINTS[endpoint]
    started = life[life["cycles"] > 0]
    return {g: (sub[time_col].to_numpy(float), sub[flag_col].to_numpy(bool))
            for g, sub in started.groupby("group", sort=True)}


def pooled_shape(groups, default: float = 2.0) -> float:
    """Weibull shape of all groups pooled; ``default`` when there are < 2 failures."""
    t = np.concatenate([g[0] for g in groups.values()])
    d = np.concatenate([g[1] for g in groups.values()])
    if d.sum() < 2:
        return default
    try:
        return fit_weibull_mle(t, d).shape
    except ValueError:
        return default


def try_weibull(t: np.ndarray, d: np.ndarray):
    """MLE fit, or None when the data cannot determine it (e.g. all failures tied at the end)."""
    if d.sum() < 2:
        return None
    try:
        return fit_weibull_mle(t, d)
    except ValueError:
        return None


def fit_table(groups, *, assumed_shape: float = 2.0, level: float = 0.90) -> pd.DataFrame:
    """Weibull MLE per group, or a Weibayes bound when the MLE is not estimable."""
    rows = []
    for g, (t, d) in groups.items():
        row = {"group": g, "drives": len(t), "failures": int(d.sum())}
        f = try_weibull(t, d)
        if f is not None:
            lo_s, hi_s = f.interval("shape", level)
            lo_t, hi_t = f.interval("scale", level)
            row.update(shape=f.shape, shape_lo=lo_s, shape_hi=hi_s, scale=f.scale,
                       scale_lo=lo_t, scale_hi=hi_t, mttf=f.mttf, b10=f.quantile(0.10),
                       mrl_at_b10=f.mrl(f.quantile(0.10)), loglik_weibull=f.loglik)
            try:
                ln = fit_lognormal_mle(t, d)
                row.update(lognormal_mu=ln.mu, lognormal_sigma=ln.sigma,
                           loglik_lognormal=ln.loglik)
            except ValueError:
                pass
        else:
            row.update(assumed_shape=assumed_shape,
                       scale_lower_bound=weibayes_lower_bound(t, d, assumed_shape, level))
        rows.append(row)
    return pd.DataFrame(rows)


def analyze_campaign(campaign: Campaign, out_dir: Path, *, threshold: float = 0.5,
                     level: float = 0.90) -> dict[str, Path]:
    style.apply()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    life = life_table(campaign)
    life.to_csv(out / "life_table.csv", index=False)
    written["life_table"] = out / "life_table.csv"

    for endpoint in ENDPOINTS:
        groups = group_data(life, endpoint)
        if groups:
            table = fit_table(groups, assumed_shape=pooled_shape(groups), level=level)
            path = out / f"weibull_{endpoint}.csv"
            table.to_csv(path, index=False)
            written[f"weibull_{endpoint}"] = path

    groups = group_data(life, "hard")
    fits = {g: f for g, (t, d) in groups.items() if (f := try_weibull(t, d)) is not None}
    if len(fits) >= 2:
        test = lr_test_common_shape({g: groups[g] for g in fits})
        pd.DataFrame([test]).to_csv(out / "common_shape_test.csv", index=False)
        written["common_shape_test"] = out / "common_shape_test.csv"
    fig, axes = plt.subplots(1, 2, figsize=(style.PAGE_W, 2.4))
    if any(d.any() for _, d in groups.values()):
        weibull_probability_plot(axes[0], groups, fits)
    km_plot(axes[1], groups, level)
    written["fig_life"] = style.save(fig, out / "fig_life")[0]

    units = recurrent_units(campaign)
    fig, ax = plt.subplots(figsize=(style.COLUMN_W, 2.2))
    mcf_plot(ax, {g: mean_cumulative_function(u, level) for g, u in units.items()})
    written["fig_mcf"] = style.save(fig, out / "fig_mcf")[0]

    cycles = cycles_frame(campaign)
    if not cycles.empty:
        paths = normalised_paths(cycles, baseline_cycles=campaign.failure.baseline_cycles)
        deg = fit_paths(paths, threshold)
        deg.to_csv(out / "degradation.csv", index=False)
        written["degradation"] = out / "degradation.csv"
        fig, ax = plt.subplots(figsize=(style.COLUMN_W, 2.2))
        degradation_plot(ax, paths, deg, threshold, workload=campaign.rotation[-1])
        written["fig_degradation"] = style.save(fig, out / "fig_degradation")[0]

    hard = life[life["cycles"] > 0]
    factors = [f for f in ("brand", "capacity_gb") if hard[f].nunique() > 1]
    if factors and hard["hard_failed"].sum() >= len(factors) + 3:
        x, names = design_matrix(hard, factors)
        model = fit_weibull_regression(hard["t_hard"], hard["hard_failed"], x, names)
        model.table(level).to_csv(out / "regression.csv", index=False)
        written["regression"] = out / "regression.csv"

    failed = ~cycles["outcome"].isin(["pass", "aborted", "port_fault"]) if not cycles.empty \
        else pd.Series(dtype=bool)
    if not cycles.empty and failed.sum() >= 3:
        covariates = [c for c in ("brand", "capacity_gb") if cycles[c].nunique() > 1]
        x, y, names, clusters = per_cycle_design(cycles, covariates=covariates)
        fit = fit_cloglog(x, y, names, clusters=clusters)
        fit.table(level).to_csv(out / "per_cycle_hazard.csv", index=False)
        written["per_cycle_hazard"] = out / "per_cycle_hazard.csv"

    events = events_frame(campaign)
    if not events.empty and {"detail.host", "detail.port"} <= set(events.columns):
        failures = events[events["type"] == "cycle_failure"].rename(
            columns={"detail.host": "host", "detail.port": "port"})
        flagged = flag_common_cause(failures[["drive_id", "cycle", "code", "time", "host",
                                              "port"]])
        flagged.to_csv(out / "common_cause.csv", index=False)
        written["common_cause"] = out / "common_cause.csv"

    written["workbook"] = write_workbook(campaign, out / f"{campaign.name}.xlsx")
    return written
