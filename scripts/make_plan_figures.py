"""Generate every figure of the test plan.

    python scripts/make_plan_figures.py --out ../Project/figures

Writes fig_rig, fig_cycle, fig_planning, fig_rehearsal and fig_schedule as PDF
(vector, for LaTeX) and PNG (preview). The planning curves use the speed
assumptions in ``planning_assumptions.py``; the rehearsal figure analyses
*simulated* logs and is labelled as such.
"""

from __future__ import annotations

import argparse
import tempfile
import textwrap
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from flashrel.analysis.degradation import fit_paths, normalised_paths
from flashrel.analysis.lifedata import cycles_frame, life_table, recurrent_units
from flashrel.analysis.nonparametric import mean_cumulative_function
from flashrel.analysis.parametric import fit_weibull_mle
from flashrel.analysis.planning import (
    cycle_hours,
    cycles_in,
    prob_at_least,
    rotation_hours,
    units_for_failures,
    weibull_cdf,
)
from flashrel.analysis.report import group_data
from flashrel.analysis.simulate import REHEARSAL_TRUTH, simulate_campaign
from flashrel.config import load_campaign
from flashrel.viz import diagrams, style
from flashrel.viz.plots import (
    degradation_plot,
    km_plot,
    mcf_plot,
    units_needed_plot,
    weibull_probability_plot,
)

from planning_assumptions import EXTENSION_DELAY_DAYS, NOMINAL, TEST_DAYS

REPO = Path(__file__).resolve().parents[1]
FORMATS: tuple[str, ...] = ("pdf", "png")


def fig_rig(out: Path) -> None:
    style.save(diagrams.rig_figure(), out / "fig_rig", FORMATS, width=style.PAGE_W)


def fig_cycle(out: Path) -> None:
    style.save(diagrams.cycle_flowchart(), out / "fig_cycle", FORMATS, width=style.PAGE_W)


def fig_planning(out: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 2.05),
                             gridspec_kw={"wspace": 0.42})

    # a: full-fill cycles per day against sustained write speed
    ax = axes[0]
    speeds = np.linspace(0.5, 15, 300)
    for lo, hi, x, ha, label in ((0.5, 2.0, 2.15, "left", "small\nfiles"),
                                 (4.0, 10.0, 7.0, "center", "medium and\nlarge files")):
        ax.axvspan(lo, hi, color=style.PAPER, lw=0)
        ax.text(x, 0.97, label, transform=ax.get_xaxis_transform(), ha=ha, va="top",
                fontsize=5.4, color=style.GRAY)
    for cap, ls in ((8, "-"), (16, (0, (4, 2)))):
        per_day = [24 / cycle_hours(cap, s, 20.0) for s in speeds]
        ax.plot(speeds, per_day, color=style.INK, ls=ls, lw=1.0, label=f"{cap} GB drive")
    ax.set_xlim(0.5, 15)
    ax.set_ylim(0, 110)
    ax.set_xlabel("Sustained write speed (MB/s)")
    ax.set_ylabel("Full-fill cycles per day")
    ax.legend(loc="lower right", fontsize=6)
    style.panel_label(ax, "a", x=-0.27)

    # b: expected hard failures by the end of the test against characteristic life
    ax = axes[1]
    theta = np.geomspace(100, 20_000, 400)
    # (days on test, capacity) -> drives; the 31 extension drives start ten days later
    days_ext = TEST_DAYS - EXTENSION_DELAY_DAYS
    designs = {"Phase 1, 9 drives": {(TEST_DAYS, 8): 7, (TEST_DAYS, 16): 2},
               "full design, 40 drives": {(TEST_DAYS, 8): 7, (TEST_DAYS, 16): 2,
                                          (days_ext, 8): 13, (days_ext, 16): 18}}
    tau = {key: cycles_in(key[0], rotation_hours(key[1], NOMINAL))
           for counts in designs.values() for key in counts}
    colors = {"Phase 1, 9 drives": style.GROUP_COLORS["A16"],
              "full design, 40 drives": style.GROUP_COLORS["S16"]}
    ax.axvspan(100, 1000, color=style.PAPER, lw=0)
    ax.text(316, 0.04, "range reported\nfor cheap USB\ndrives", transform=ax.get_xaxis_transform(),
            ha="center", va="bottom", fontsize=5.4, color=style.GRAY)
    for name, counts in designs.items():
        def expected(shape):
            return sum(n * weibull_cdf(tau[key], shape, theta) for key, n in counts.items())
        ax.fill_between(theta, expected(1.5), expected(3.0), color=colors[name], alpha=0.16,
                        lw=0)
        ax.plot(theta, expected(2.0), color=colors[name], lw=1.1, label=name)
    ax.set_xscale("log")
    ax.set_xlim(100, 20_000)
    ax.set_ylim(0, 42)
    ax.set_xlabel(r"Characteristic life $\theta$ (cycles)")
    ax.set_ylabel("Expected hard failures by 1 Dec")
    ax.legend(loc="upper right", fontsize=5.8, bbox_to_anchor=(1.03, 1.02))
    style.panel_label(ax, "b", x=-0.27)

    # c: drives needed to observe 20 failures with 90 % assurance
    ax = axes[2]
    p = np.linspace(0.2, 1.0, 161)
    n_needed = np.array([units_for_failures(20, x, 0.90) for x in p], float)
    units_needed_plot(ax, p, n_needed, 20, reference=((0.5, f"{int(n_needed[60])}"),
                                                      (0.8, f"{int(n_needed[120])}")))
    style.panel_label(ax, "c", x=-0.27)
    style.save(fig, out / "fig_planning", FORMATS, width=style.PAGE_W)


def _rehearsal_campaign(root: Path):
    """Two brands x two capacities x ten drives, written as YAML and loaded."""
    groups = {
        "S8": ("SanDisk", 8, "plastic"), "S16": ("SanDisk", 16, "plastic"),
        "A8": ("ABLAZE", 8, "metal"), "A16": ("ABLAZE", 16, "metal"),
    }
    inv = ["groups:"]
    for code, (brand, cap, housing) in groups.items():
        inv.append(f"  {code}: {{brand: {brand}, model: rehearsal, capacity_gb: {cap}, "
                   f"interface: USB 2.0, housing: {housing}, listed_write: '-', "
                   f"unit_price_usd: 0}}")
    inv.append("units:")
    for code in groups:
        ids = ", ".join(f"{code}-{i:02d}" for i in range(1, 11))
        inv.append(f"  {code}: [{ids}]")
    (root / "inventory.yaml").write_text("\n".join(inv) + "\n", encoding="utf-8")
    assign = "\n".join(f"  {code}-{i:02d}: {{host: H, port: P{code}{i}}}"
                       for code in groups for i in range(1, 11))
    (root / "campaign.yaml").write_text(textwrap.dedent("""\
        campaign: rehearsal
        seed: 7
        data_dir: data
        workloads:
          small:  {min_size: 4KiB, max_size: 256KiB}
          medium: {min_size: 1MiB, max_size: 64MiB}
          large:  {min_size: 512MiB, max_size: 2GiB}
        rotation: [small, medium, large]
        assignments:
        """) + assign + "\n", encoding="utf-8")
    return load_campaign(root / "campaign.yaml")


def fig_rehearsal(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        campaign = _rehearsal_campaign(Path(tmp))
        horizon = {}
        for code, group in campaign.inventory.groups.items():
            truth = REHEARSAL_TRUTH[code]
            speeds = {w: (truth.write_mbps[w], truth.read_mbps[w]) for w in campaign.rotation}
            horizon[code] = int(cycles_in(TEST_DAYS, rotation_hours(group.capacity_gb, speeds)))
        simulate_campaign(campaign, horizon, seed=11)
        life = life_table(campaign)
        groups = group_data(life, "hard")
        fits = {g: fit_weibull_mle(t, d) for g, (t, d) in groups.items() if d.sum() >= 2}
        cycles = cycles_frame(campaign)
        paths = normalised_paths(cycles)
        deg = fit_paths(paths, 0.5)
        mcf = {g: mean_cumulative_function(u) for g, u in recurrent_units(campaign).items()}

    order = ("S8", "S16", "A8", "A16")
    groups = {g: groups[g] for g in order if g in groups}
    fig, axes = plt.subplots(1, 4, figsize=(style.PAGE_W, 2.05), gridspec_kw={"wspace": 0.58})
    weibull_probability_plot(axes[0], groups, fits, t_range=(60, 6000))
    axes[0].get_legend().remove()
    km_plot(axes[1], groups)
    axes[1].get_legend().remove()
    axes[1].set_xlim(0, 1600)
    # two drives per 8 GB group whose large-file paths cross the threshold
    crossing = sorted(deg.loc[(deg["workload"] == "large") & deg["pseudo_failure"], "drive_id"])
    shown = [d for d in crossing if d.startswith("S8-")][:2] + \
            [d for d in crossing if d.startswith("A8-")][:2]
    degradation_plot(axes[2], paths, deg, 0.5, workload="large", drives=shown)
    mcf_plot(axes[3], {g: mcf[g] for g in order if g in mcf})
    axes[3].get_legend().remove()
    axes[3].set_ylabel("Mean cumulative\nsoft failures")
    for ax, letter in zip(axes, "abcd"):
        style.panel_label(ax, letter, x=-0.36)
    handles = [plt.Line2D([], [], color=style.GROUP_COLORS[g], marker=style.GROUP_MARKERS[g],
                          mfc="white", lw=1.1, ms=3.4, label=style.GROUP_LABELS[g])
               for g in order]
    fig.legend(handles=handles, loc="upper center", ncol=4, bbox_to_anchor=(0.47, 1.10),
               fontsize=6.3, columnspacing=1.6)
    fig.text(0.995, 1.085, "SIMULATED DATA · pipeline rehearsal", ha="right", va="center",
             fontsize=5.6, color=style.FAIL, fontweight="bold")
    style.save(fig, out / "fig_rehearsal", FORMATS, width=style.PAGE_W)


def fig_schedule(out: Path) -> None:
    T, M = diagrams.Task, diagrams.Milestone
    d = date
    tasks = [
        T("Literature review and test plan", d(2026, 10, 3), d(2026, 10, 9), "all", "prep"),
        T("Harness code, self-test, dry runs", d(2026, 10, 3), d(2026, 10, 9), "GW", "prep"),
        T("Drive intake: label, enroll, screen", d(2026, 10, 5), d(2026, 10, 7), "SW", "prep"),
        T("Rig build: docks, port labels, probes", d(2026, 10, 5), d(2026, 10, 9), "SW", "prep"),
        T("Pilot: 2 drives, fault injection", d(2026, 10, 6), d(2026, 10, 11), "GW, SW", "test"),
        T("Phase 1: 9 drives cycling 24/7", d(2026, 10, 12), d(2026, 12, 1), "all", "test"),
        T("Daily checks, weekly data review", d(2026, 10, 12), d(2026, 12, 1), "rota",
          "monitor"),
        T("Proposal presentation slides", d(2026, 10, 13), d(2026, 10, 19), "all", "analysis"),
        T("Phase 2 (if approved): order, enroll, run", d(2026, 10, 21), d(2026, 12, 1), "all",
          "test"),
        T("Interim analysis", d(2026, 11, 9), d(2026, 11, 13), "MA", "analysis"),
        T("Final analysis and report", d(2026, 12, 1), d(2026, 12, 11), "all", "analysis"),
    ]
    milestones = [M("drives\narrive", d(2026, 10, 5)), M("plan\nreview", d(2026, 10, 10)),
                  M("proposal\ntalk", d(2026, 10, 20)), M("interim\nreview", d(2026, 11, 13)),
                  M("test stop\n(censoring)", d(2026, 12, 1))]
    fig = diagrams.schedule_figure(tasks, milestones, d(2026, 10, 3), d(2026, 12, 13))
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
