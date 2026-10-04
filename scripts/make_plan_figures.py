"""Generate every figure of the test plan.

    python scripts/make_plan_figures.py --out ../Project/figures --formats pdf

Writes fig_rig, fig_cycle, fig_planning, fig_rehearsal and fig_schedule as PDF
(vector, for LaTeX) and PNG (preview). The planning curves use the speed
assumptions in ``planning_assumptions.py``; the rehearsal figure analyses
*simulated* logs of the real campaign and is labelled as such.
"""

from __future__ import annotations

import argparse
import dataclasses
import tempfile
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

from flashrel.analysis.degradation import fit_paths, normalised_paths
from flashrel.analysis.lifedata import cycles_frame, life_table, recurrent_units
from flashrel.analysis.nonparametric import mean_cumulative_function
from flashrel.analysis.planning import cycle_hours, cycles_in, rotation_hours, weibull_cdf
from flashrel.analysis.report import group_data, try_weibull, weibayes_bounds
from flashrel.analysis.simulate import REHEARSAL_TRUTH, simulate_campaign
from flashrel.config import Campaign, load_campaign
from flashrel.viz import diagrams, style
from flashrel.viz.plots import (
    BOUND_DASH,
    degradation_plot,
    km_plot,
    mcf_plot,
    weibull_probability_plot,
)

from planning_assumptions import NOMINAL, TEST_DAYS

REPO = Path(__file__).resolve().parents[1]
CAMPAIGN = REPO / "configs" / "campaign.yaml"
FORMATS: tuple[str, ...] = ("pdf", "png")
NINE = "#2B5C8A"          # the nine-drive test in the planning panels
REHEARSAL_SEED = 5
GROUP_ORDER = ("S8", "A8", "A16")


def fig_rig(out: Path) -> None:
    style.save(diagrams.rig_figure(), out / "fig_rig", FORMATS, width=style.PAGE_W)


def fig_cycle(out: Path) -> None:
    style.save(diagrams.cycle_flowchart(), out / "fig_cycle", FORMATS, width=style.PAGE_W)


# -- planning --------------------------------------------------------------------------------
def drives_by_capacity(campaign: Campaign) -> dict[float, int]:
    """Number of assigned drives per nominal capacity, e.g. {8: 7, 16: 2}."""
    counts: dict[float, int] = {}
    for drive_id in campaign.assignments:
        cap = campaign.inventory[drive_id].capacity_gb
        counts[cap] = counts.get(cap, 0) + 1
    return dict(sorted(counts.items()))


def _band_label(ax, x: float, text: str, *, y: float = 0.97, va: str = "top",
                ha: str = "center") -> None:
    ax.text(x, y, text, transform=ax.get_xaxis_transform(), ha=ha, va=va, fontsize=5.4,
            color=style.GRAY, linespacing=1.25)


def fig_planning(out: Path) -> None:
    drives = drives_by_capacity(load_campaign(CAMPAIGN))
    hours = {cap: rotation_hours(cap, NOMINAL) for cap in drives}
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 1.95), gridspec_kw={"wspace": 0.5})

    # a: full-fill cycles per day against the sustained write speed
    ax = axes[0]
    speeds = np.linspace(0.5, 15, 300)
    for lo, hi in ((0.5, 2.0), (4.0, 10.0)):
        ax.axvspan(lo, hi, color=style.PAPER, lw=0)
    _band_label(ax, 2.2, "small\nfiles", ha="left")
    _band_label(ax, 7.0, "medium and\nlarge files")
    for cap, ls in ((8, "-"), (16, (0, (4, 2)))):
        per_day = np.array([24 / cycle_hours(cap, s, 20.0) for s in speeds])
        ax.plot(speeds, per_day, color=style.INK, ls=ls, lw=1.0)
        ax.text(14.8, per_day[-1] + 3.5, f"{cap} GB", ha="right", va="bottom", fontsize=5.8)
    ax.set_xlim(0.5, 15)
    ax.set_ylim(0, 112)
    ax.set_xlabel("Sustained write speed (MB/s)")
    ax.set_ylabel("Full-fill cycles per day")
    style.panel_label(ax, "a", x=-0.3)

    # b: expected hard failures by the stop time against the characteristic life
    ax = axes[1]
    theta = np.geomspace(100, 10_000, 400)
    tau = {cap: cycles_in(TEST_DAYS, hours[cap]) for cap in drives}

    def expected(shape, th=theta):
        return sum(n * weibull_cdf(tau[cap], shape, th) for cap, n in drives.items())

    ax.axvspan(100, 1000, color=style.PAPER, lw=0)
    _band_label(ax, 316, "failures in\nearlier tests", y=0.04, va="bottom")
    ax.fill_between(theta, expected(1.5), expected(3.0), color=NINE, alpha=0.14, lw=0)
    ax.plot(theta, expected(2.0), color=NINE, lw=1.1)
    for th in (300, 1000):
        e = float(expected(2.0, th))
        ax.plot(th, e, marker="o", ms=3.0, mfc="white", mec=NINE, mew=0.8, zorder=3)
        ax.annotate(f"{e:.1f}", (th, e), xytext=(4, 3), textcoords="offset points",
                    fontsize=5.8, color=NINE)
    n = sum(drives.values())
    ax.set_xscale("log")
    ax.set_xlim(100, 10_000)
    ax.set_ylim(0, n + 0.6)
    ax.set_yticks(range(0, n + 1, 3))
    ax.set_xlabel(r"Characteristic life $\theta$ (cycles)")
    ax.set_ylabel("Expected hard failures by 18 Oct")
    style.panel_label(ax, "b", x=-0.3)

    # c: what the test demonstrates if no drive fails (Weibayes, r = 0, 90 %)
    ax = axes[2]
    days = np.linspace(0.25, 14, 200)
    chi2 = stats.chi2.ppf(0.90, 2)
    shades = {1.5: style.MIST, 2.0: style.INK, 3.0: style.GRAY}
    for shape, color in shades.items():
        total = sum(n_ * cycles_in(days, hours[cap]) ** shape for cap, n_ in drives.items())
        bound = (2 * total / chi2) ** (1 / shape)
        ax.plot(days, bound, color=color, lw=1.1 if shape == 2.0 else 0.9)
        ax.text(14.2, bound[-1], f"$\\gamma$ = {shape:g}", va="center", ha="left",
                fontsize=5.6, color=color)
        if shape == 2.0:
            at_stop = float(np.interp(TEST_DAYS, days, bound))
            ax.plot(TEST_DAYS, at_stop, marker="o", ms=3.0, mfc="white", mec=style.INK,
                    mew=0.8, zorder=3)
            ax.annotate(f"{at_stop:.0f}", (TEST_DAYS, at_stop), xytext=(-4, 4),
                        textcoords="offset points", ha="right", fontsize=5.8)
    ax.axvline(TEST_DAYS, color=style.FAIL, lw=0.6, ls=(0, (1, 1.5)))
    _band_label(ax, TEST_DAYS - 0.3, "stop\n18 Oct", y=0.04, va="bottom", ha="right")
    ax.text(0.03, 0.97, "no failures,\n90 % confidence", transform=ax.transAxes, ha="left",
            va="top", fontsize=5.4, color=style.GRAY, linespacing=1.25)
    ax.set_xlim(0, 14)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Days on test")
    ax.set_ylabel(r"Demonstrated $\theta$ (cycles)")
    style.panel_label(ax, "c", x=-0.3)
    style.save(fig, out / "fig_planning", FORMATS, width=style.PAGE_W)


# -- rehearsal on simulated logs ------------------------------------------------------------
def _simulated_analysis(root: Path, seed: int):
    campaign = dataclasses.replace(load_campaign(CAMPAIGN), data_dir=root)
    horizon = {}
    for code, group in campaign.inventory.groups.items():
        truth = REHEARSAL_TRUTH[code]
        speeds = {w: (truth.write_mbps[w], truth.read_mbps[w]) for w in campaign.rotation}
        horizon[code] = int(cycles_in(TEST_DAYS, rotation_hours(group.capacity_gb, speeds)))
    simulate_campaign(campaign, horizon, seed=seed)
    groups = group_data(life_table(campaign), "hard")
    groups = {g: groups[g] for g in GROUP_ORDER if g in groups}
    fits = {g: f for g, (t, d) in groups.items() if (f := try_weibull(t, d)) is not None}
    paths = normalised_paths(cycles_frame(campaign))
    mcf = {g: mean_cumulative_function(u) for g, u in recurrent_units(campaign).items()}
    return groups, fits, weibayes_bounds(groups, fits), paths, fit_paths(paths, 0.5), mcf


def _crossing_drives(deg, per_group: int = 2, limit: int = 4) -> list[str]:
    """Up to ``limit`` drives whose large-file path crosses the threshold, spread over groups."""
    crossing = sorted(deg.loc[(deg["workload"] == "large") & deg["pseudo_failure"], "drive_id"])
    chosen: list[str] = []
    for g in GROUP_ORDER:
        chosen += [d for d in crossing if d.split("-")[0] == g][:per_group]
    return chosen[:limit]


def fig_rehearsal(out: Path, seed: int = REHEARSAL_SEED) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        groups, fits, bounds, paths, deg, mcf = _simulated_analysis(Path(tmp), seed)

    fig, axes = plt.subplots(1, 4, figsize=(style.PAGE_W, 1.95), gridspec_kw={"wspace": 0.6})
    weibull_probability_plot(axes[0], groups, fits, t_range=(20, 2000), bounds=bounds)
    axes[0].get_legend().remove()
    km_plot(axes[1], groups)
    axes[1].get_legend().remove()
    degradation_plot(axes[2], paths, deg, 0.5, workload="large", drives=_crossing_drives(deg))
    mcf_plot(axes[3], {g: mcf[g] for g in GROUP_ORDER if g in mcf})
    axes[3].get_legend().remove()
    axes[3].set_ylabel("Mean cumulative\nsoft failures")
    x_max = max(float(t.max()) for t, _ in groups.values()) * 1.05
    for ax in axes[1:]:
        ax.set_xlim(0, x_max)
    for ax, letter in zip(axes, "abcd"):
        style.panel_label(ax, letter, x=-0.38)

    handles = [plt.Line2D([], [], color=style.GROUP_COLORS[g], marker=style.GROUP_MARKERS[g],
                          mfc="white", lw=1.1, ms=3.4, label=style.GROUP_LABELS[g])
               for g in GROUP_ORDER]
    handles += [plt.Line2D([], [], color=style.INK, lw=1.0, label="ML fit"),
                plt.Line2D([], [], color=style.INK, lw=0.9, ls=BOUND_DASH,
                           label="90 % lower bound")]
    fig.legend(handles=handles, loc="upper left", ncol=5, bbox_to_anchor=(0.02, 1.11),
               fontsize=6.0, columnspacing=1.5, handlelength=1.8)
    fig.text(0.995, 1.075, "simulated data", ha="right", va="center", fontsize=6.0,
             color=style.GRAY, fontstyle="italic")
    style.save(fig, out / "fig_rehearsal", FORMATS, width=style.PAGE_W)


# -- schedule --------------------------------------------------------------------------------
def fig_schedule(out: Path) -> None:
    T, M, dt = diagrams.Task, diagrams.Milestone, datetime
    checks = tuple(dt(2026, 10, day, 9) for day in range(7, 18))
    tasks = [
        T("Test plan and software", dt(2026, 10, 3, 9), dt(2026, 10, 5, 18), "all", "prep"),
        T("Intake: label, format, enroll", dt(2026, 10, 5, 9), dt(2026, 10, 5, 18), "SW",
          "prep"),
        T("Pilot on two drives", dt(2026, 10, 5, 18), dt(2026, 10, 6, 9), "GW", "test"),
        T("Test: nine drives, 24 h a day", dt(2026, 10, 6, 9), dt(2026, 10, 18, 9), "", "test"),
        T("Daily status check", checks[0], checks[-1], "all, in turn", "check", marks=checks),
        T("Export and analysis", dt(2026, 10, 18, 9), dt(2026, 10, 18, 18), "MA", "report"),
        T("Slides", dt(2026, 10, 18, 12), dt(2026, 10, 19, 22), "all", "report"),
    ]
    milestones = [M("drives arrive", dt(2026, 10, 5, 9), "right"),
                  M("all drives start", dt(2026, 10, 6, 9), "left"),
                  M("stop, 09:00", dt(2026, 10, 18, 9), "right"),
                  M("presentation", dt(2026, 10, 20, 12), "center")]
    fig = diagrams.schedule_figure(tasks, milestones, dt(2026, 10, 3), dt(2026, 10, 21))
    style.save(fig, out / "fig_schedule", FORMATS, width=style.PAGE_W)


def main() -> None:
    global FORMATS
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO / "build" / "figures"))
    parser.add_argument("--only", nargs="*", help="subset, e.g. rig planning")
    parser.add_argument("--formats", nargs="+", default=list(FORMATS),
                        help="file types to write (default: pdf png)")
    args = parser.parse_args()
    FORMATS = tuple(args.formats)
    out = Path(args.out)
    style.apply()
    builders = {"rig": fig_rig, "cycle": fig_cycle, "planning": fig_planning,
                "rehearsal": fig_rehearsal, "schedule": fig_schedule}
    for name, build in builders.items():
        if args.only and name not in args.only:
            continue
        build(out)
        print(f"wrote {out / ('fig_' + name)}.pdf")


if __name__ == "__main__":
    main()
