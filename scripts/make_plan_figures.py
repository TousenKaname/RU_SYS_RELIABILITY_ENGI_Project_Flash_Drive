"""Generate every figure of the test plan.

    python scripts/make_plan_figures.py --out ../Project/figures --formats pdf

Writes fig_rig, fig_cycle, fig_planning and fig_schedule as PDF (vector, for
LaTeX) and PNG (preview). The planning curves use the speed assumptions in
``planning_assumptions.py``.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

from flashrel.analysis.planning import cycle_hours, cycles_in, rotation_hours, weibull_cdf
from flashrel.config import Campaign, load_campaign
from flashrel.viz import diagrams, style

from planning_assumptions import NOMINAL, TEST_DAYS

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
    style.panel_label(ax, "b", x=-0.3)

    # c: what the test demonstrates if no drive fails (Weibayes, r = 0, 90 %)
    ax = axes[2]
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
    ax.text(0.03, 0.97, "no failures,\n90 % confidence", transform=ax.transAxes, ha="left",
            va="top", fontsize=5.4, color=style.GRAY, linespacing=1.25)
    ax.set_xlim(0, 7)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Days on test")
    ax.set_ylabel(r"Lower bound on $\theta$ (cycles)")
    style.panel_label(ax, "c", x=-0.3)
    style.save(fig, out / "fig_planning", FORMATS, width=style.PAGE_W)


# -- schedule --------------------------------------------------------------------------------
def fig_schedule(out: Path) -> None:
    T, M, dt = diagrams.Task, diagrams.Milestone, datetime
    tasks = [
        T("Intake: format, enroll, self-test", dt(2026, 10, 23, 9), dt(2026, 10, 23, 10), "",
          "prep"),
        T("Pilot on two drives", dt(2026, 10, 23, 10), dt(2026, 10, 23, 12), "", "test"),
        T("Test: eight drives, 24 h", dt(2026, 10, 23, 12), dt(2026, 10, 24, 12), "", "test"),
        T("Post-test check, export, analysis", dt(2026, 10, 24, 12), dt(2026, 10, 24, 15), "",
          "report"),
    ]
    milestones = [M("all drives start", dt(2026, 10, 23, 12), "right"),
                  M("stop, 12:00", dt(2026, 10, 24, 12), "left")]
    fig = diagrams.schedule_figure(tasks, milestones, dt(2026, 10, 23), dt(2026, 10, 25))
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
                "schedule": fig_schedule}
    for name, build in builders.items():
        if args.only and name not in args.only:
            continue
        build(out)
        print(f"wrote {out / ('fig_' + name)}.pdf")


if __name__ == "__main__":
    main()
