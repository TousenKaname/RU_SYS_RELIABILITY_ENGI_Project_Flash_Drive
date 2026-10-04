"""Data figures: probability plots, reliability curves, degradation, MCF."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
from matplotlib.ticker import FixedLocator, NullLocator

from flashrel.analysis.nonparametric import kaplan_meier, median_ranks
from flashrel.analysis.parametric import WeibullFit
from flashrel.viz.style import FAIL, GROUP_COLORS, GROUP_LABELS, GROUP_MARKERS, HAIRLINE, INK

WEIBULL_TICKS = (0.01, 0.05, 0.2, 0.5, 0.9, 0.99)
BAND_ALPHA = 0.10
BOUND_DASH = (0, (3.0, 1.8))


def _w(p):
    """Weibull-paper ordinate ln(-ln(1 - p)), clipped away from 0 and 1."""
    p = np.clip(np.asarray(p, float), 1e-12, 1 - 1e-12)
    return np.log(-np.log1p(-p))


def weibull_probability_plot(ax, groups: Mapping[str, tuple[Sequence[float], Sequence[bool]]],
                             fits: Mapping[str, WeibullFit],
                             t_range: tuple[float, float] | None = None,
                             bounds: Mapping[str, tuple[float, float]] | None = None) -> None:
    """Weibull paper: failures at median ranks, suspensions as ticks, MLE lines.

    Each fitted line is drawn only over its own group's data (with a small
    margin), so lines do not suggest knowledge far outside the observations.
    ``bounds`` maps a group without a fit to (shape, theta lower bound); its
    Weibayes line is dashed. The horizontal hairline marks 63.2 %, where t
    equals theta.
    """
    all_t = np.concatenate([np.asarray(t, float) for t, _ in groups.values()])
    lo, hi = t_range or (all_t.min() * 0.6, all_t.max() * 1.6)
    for g, (t, d) in groups.items():
        color = GROUP_COLORS.get(g, INK)
        ranks = median_ranks(t, d)
        ax.plot(ranks["time"], _w(ranks["F"]), ls="none", marker=GROUP_MARKERS.get(g, "o"),
                mfc="white", mec=color, mew=0.8, ms=3.4, zorder=3)
        tt = np.asarray(t, float)
        grid = np.geomspace(max(lo, tt.min() * 0.7), min(hi, tt.max() * 1.5), 120)
        if g in fits:
            f = fits[g]
            ax.plot(grid, _w(f.cdf(grid)), color=color, lw=1.0, zorder=2,
                    label=f"{GROUP_LABELS.get(g, g)}  ($\\gamma$={f.shape:.1f}, "
                          f"$\\theta$={f.scale:,.0f})")
        elif bounds and g in bounds:
            shape, theta_lower = bounds[g]
            grid = np.geomspace(max(lo, tt.min() * 0.25), min(hi, theta_lower * 1.8), 120)
            cdf = 1.0 - np.exp(-(grid / theta_lower) ** shape)
            ax.plot(grid, _w(cdf), color=color, lw=0.9, ls=BOUND_DASH, zorder=2,
                    label=f"{GROUP_LABELS.get(g, g)}  ($\\theta \\geq$ {theta_lower:,.0f})")
        susp = np.asarray(t, float)[~np.asarray(d, bool)]
        if susp.size:
            ax.plot(susp, np.full(susp.size, _w(0.0035)), ls="none", marker="|", ms=4,
                    mew=0.8, color=color, zorder=3)
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ax.set_ylim(_w(0.0025), _w(0.995))
    ax.yaxis.set_major_locator(FixedLocator(_w(WEIBULL_TICKS)))
    ax.set_yticklabels([f"{100 * p:g}" for p in WEIBULL_TICKS])
    ax.yaxis.set_minor_locator(NullLocator())
    ax.axhline(_w(0.632), color=HAIRLINE, lw=0.6, zorder=1)
    ax.set_xlabel("Cycles to hard failure")
    ax.set_ylabel("Unreliability (%)")
    ax.legend(loc="upper left", fontsize=5.8, handlelength=1.2)


def km_plot(ax, groups: Mapping[str, tuple[Sequence[float], Sequence[bool]]],
            level: float = 0.90) -> None:
    """Kaplan-Meier reliability per group with pointwise bands and censoring ticks."""
    for g, (t, d) in groups.items():
        color = GROUP_COLORS.get(g, INK)
        km = kaplan_meier(t, d, level)
        t_end = float(np.max(t))

        def steps(column: str, start: float = 1.0) -> np.ndarray:
            values = km[column].to_numpy(float)
            tail = values[-1] if values.size else start
            return np.concatenate([[start], values, [tail]])

        xs = np.concatenate([[0.0], km["time"].to_numpy(float), [t_end]])
        ys, lo, hi = steps("reliability"), steps("lower"), steps("upper")
        ax.fill_between(xs, lo, hi, step="post", color=color, alpha=BAND_ALPHA, lw=0)
        ax.step(xs, ys, where="post", color=color, lw=1.1, label=GROUP_LABELS.get(g, g))
        tt, dd = np.asarray(t, float), np.asarray(d, bool)
        for c in tt[~dd]:
            r = ys[np.searchsorted(xs, c, side="right") - 1]
            ax.plot(c, r, marker="|", color=color, ms=4, mew=0.8)
    ax.set_ylim(-0.02, 1.04)
    ax.set_xlim(left=0)
    ax.set_xlabel("Cycles")
    ax.set_ylabel("Reliability $R(t)$")
    ax.legend(loc="lower left", fontsize=6)


def degradation_plot(ax, paths: pd.DataFrame, fits: pd.DataFrame, threshold: float = 0.5,
                     workload: str = "large", drives: Sequence[str] | None = None) -> None:
    """Relative write throughput for one workload, with fitted paths and the threshold.

    Observed points are thin lines; each drive's log-linear fit is dashed and
    runs on to the cycle where it crosses the threshold (open circle): that
    cycle is the drive's pseudo-failure time.
    """
    sub = paths[paths["workload"] == workload]
    if drives is not None:
        sub = sub[sub["drive_id"].isin(drives)]
    x_max = 0.0
    for drive, path in sub.groupby("drive_id"):
        color = GROUP_COLORS.get(str(path["group"].iloc[0]), INK)
        ax.plot(path["cycle"], path["relative"], color=color, lw=0.6, alpha=0.9)
        row = fits[(fits["drive_id"] == drive) & (fits["workload"] == workload)]
        x_max = max(x_max, float(path["cycle"].max()))
        if not len(row) or not np.isfinite(row["slope"].iloc[0]):
            continue
        a, b = float(row["intercept"].iloc[0]), float(row["slope"].iloc[0])
        end = float(row["pseudo_time"].iloc[0]) if bool(row["pseudo_failure"].iloc[0]) \
            else float(path["cycle"].max())
        grid = np.linspace(path["cycle"].min(), end, 100)
        ax.plot(grid, np.exp(a + b * grid), color=color, lw=0.9, ls=(0, (3, 1.6)))
        if bool(row["pseudo_failure"].iloc[0]):
            ax.plot(end, threshold, marker="o", ms=3.2, mfc="white", mec=color, mew=0.9,
                    zorder=4)
            x_max = max(x_max, end)
    ax.axhline(threshold, color=FAIL, lw=0.6, ls=(0, (1, 1.5)))
    ax.text(0.98, threshold + 0.03, f"{threshold:.0%} threshold", transform=ax.get_yaxis_transform(),
            color=FAIL, fontsize=5.6, ha="right", va="bottom")
    ax.set_ylim(0, 1.25)
    ax.set_xlim(0, x_max * 1.05 if x_max else None)
    ax.set_xlabel("Cycles")
    ax.set_ylabel("Write throughput / baseline")


def mcf_plot(ax, curves: Mapping[str, pd.DataFrame]) -> None:
    for g, mcf in curves.items():
        if mcf.empty:
            continue
        color = GROUP_COLORS.get(g, INK)
        xs = np.concatenate([[0.0], mcf["time"]])
        ys = np.concatenate([[0.0], mcf["mcf"]])
        lo = np.concatenate([[0.0], mcf["lower"]])
        hi = np.concatenate([[0.0], mcf["upper"]])
        ax.fill_between(xs, lo, hi, step="post", color=color, alpha=BAND_ALPHA, lw=0)
        ax.step(xs, ys, where="post", color=color, lw=1.1, label=GROUP_LABELS.get(g, g))
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Cycles")
    ax.set_ylabel("Mean cumulative soft failures")
    ax.legend(loc="upper left", fontsize=6)
