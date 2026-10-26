"""Generate every figure of the test plan.

    python scripts/make_plan_figures.py --out ../Project/figures --formats pdf

Writes fig_rig, fig_cycle, fig_planning and fig_schedule as PDF (vector, for
LaTeX) and PNG (preview). The planning curves use the speed assumptions in
``planning_assumptions.py``.
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

from flashrel.analysis.planning import cycle_hours, cycles_in, rotation_hours, weibull_cdf
from flashrel.config import Campaign, load_campaign
from flashrel.viz import diagrams, style

from planning_assumptions import NOMINAL, PLAN_DAY, TEST_DAYS

REPO = Path(__file__).resolve().parents[1]
CAMPAIGN = REPO / "configs" / "campaign.yaml"
FORMATS: tuple[str, ...] = ("pdf", "png")
TEST = "#2B5C8A"          # the test in the planning panels


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


def planning_inputs() -> tuple[dict[float, int], dict[float, float]]:
    """Drives per capacity and the hours of one workload rotation per capacity."""
    drives = drives_by_capacity(load_campaign(CAMPAIGN))
    return drives, {cap: rotation_hours(cap, NOMINAL) for cap in drives}


def panel_rate(ax, drives: dict[float, int], hours: dict[float, float]) -> None:
    """Full-fill cycles per day against the sustained write speed."""
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


def panel_failures(ax, drives: dict[float, int], hours: dict[float, float]) -> None:
    """Expected hard failures by the stop time against the characteristic life."""
    theta = np.geomspace(10, 1000, 400)
    tau = {cap: cycles_in(TEST_DAYS, hours[cap]) for cap in drives}

    def expected(shape, th=theta):
        return sum(n * weibull_cdf(tau[cap], shape, th) for cap, n in drives.items())

    ax.axvspan(100, 1000, color=style.PAPER, lw=0)
    _band_label(ax, 316, "failures in\nearlier tests", y=0.55, va="bottom")
    ax.fill_between(theta, expected(1.5), expected(3.0), color=TEST, alpha=0.14, lw=0)
    ax.plot(theta, expected(2.0), color=TEST, lw=1.1)
    for th in (30, 100):
        e = float(expected(2.0, th))
        ax.plot(th, e, marker="o", ms=3.0, mfc="white", mec=TEST, mew=0.8, zorder=3)
        ax.annotate(f"{e:.1f}", (th, e), xytext=(4, 3), textcoords="offset points",
                    fontsize=5.8, color=TEST)
    n = sum(drives.values())
    ax.set_xscale("log")
    ax.set_xlim(10, 1000)
    ax.set_ylim(0, n + 0.6)
    ax.set_yticks(range(0, n + 1, 2))
    ax.set_xlabel(r"Characteristic life $\theta$ (cycles)")
    ax.set_ylabel("Expected hard failures in 24 h")


def panel_bound(ax, drives: dict[float, int], hours: dict[float, float]) -> None:
    """What the test demonstrates if no drive fails (Weibayes, r = 0, 90 %)."""
    days = np.linspace(0.1, 7, 200)
    chi2 = stats.chi2.ppf(0.90, 2)
    shades = {1.5: style.MIST, 2.0: style.INK, 3.0: style.GRAY}
    for shape, color in shades.items():
        total = sum(n_ * cycles_in(days, hours[cap]) ** shape for cap, n_ in drives.items())
        bound = (2 * total / chi2) ** (1 / shape)
        ax.plot(days, bound, color=color, lw=1.1 if shape == 2.0 else 0.9)
        ax.text(7.1, bound[-1], f"$\\gamma$ = {shape:g}", va="center", ha="left",
                fontsize=5.6, color=color)
        if shape == 2.0:
            at_stop = float(np.interp(TEST_DAYS, days, bound))
            ax.plot(TEST_DAYS, at_stop, marker="o", ms=3.0, mfc="white", mec=style.INK,
                    mew=0.8, zorder=3)
            ax.annotate(f"{at_stop:.0f}", (TEST_DAYS, at_stop), xytext=(5, -3),
                        textcoords="offset points", ha="left", va="top", fontsize=5.8)
    ax.axvline(TEST_DAYS, color=style.FAIL, lw=0.6, ls=(0, (1, 1.5)))
    _band_label(ax, TEST_DAYS + 0.15, "stop\n24 h", y=0.62, va="bottom", ha="left")
    # bottom right, under the lines and clear of the stop line
    ax.text(0.97, 0.04, "no failures,\n90 % confidence", transform=ax.transAxes, ha="right",
            va="bottom", fontsize=5.4, color=style.GRAY, linespacing=1.25)
    ax.set_xlim(0, 7)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Days on test")
    ax.set_ylabel(r"Lower bound on $\theta$ (cycles)")


PANELS = {"rate": panel_rate, "failures": panel_failures, "bound": panel_bound}


def fig_planning(out: Path) -> None:
    drives, hours = planning_inputs()
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 1.95), gridspec_kw={"wspace": 0.5})
    for ax, (letter, draw) in zip(axes, zip("abc", PANELS.values())):
        draw(ax, drives, hours)
        style.panel_label(ax, letter, x=-0.3)
    style.save(fig, out / "fig_planning", FORMATS, width=style.PAGE_W)


# -- schedule --------------------------------------------------------------------------------
def fig_schedule(out: Path, size: tuple[float, float] | None = None,
                 name: str = "fig_schedule") -> None:
    """The plan's Gantt chart; ``size`` (inches) draws a smaller copy, e.g. for a slide."""
    T, M = diagrams.Task, diagrams.Milestone

    def at(hours: float):
        return PLAN_DAY + timedelta(hours=hours)
    tasks = [
        T("Intake: format, enroll, self-test", at(9), at(10), "", "prep"),
        T("Pilot on two drives", at(10), at(12), "", "test"),
        T("Test: eight drives, 24 h", at(12), at(36), "", "test"),
        T("Post-test check, export, analysis", at(36), at(39), "", "report"),
    ]
    milestones = [M("all drives start", at(12), "right"), M("stop, 12:00", at(36), "left")]
    fig = diagrams.schedule_figure(tasks, milestones, PLAN_DAY, PLAN_DAY + timedelta(days=2))
    if size:
        fig.set_size_inches(size)
    style.save(fig, out / name, FORMATS, width=size[0] if size else style.PAGE_W)


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
                "schedule": fig_schedule}
    for name, build in builders.items():
        if args.only and name not in args.only:
            continue
        build(out)
        print(f"wrote {out / ('fig_' + name)}.pdf")


if __name__ == "__main__":
    main()
