"""Results of a campaign: figures, LaTeX tables and the key numbers of the final report.

    python scripts/make_results.py configs/campaign.yaml --out ../Project/results

Everything is computed from the campaign logs (``cycles.csv``, ``events.jsonl``,
``state.json``, ``intake.json``) and the supervisor records in the run folder,
so the script can be rerun at any time, also while the test is running.

Outputs in ``--out``:

* ``fig_timeline``, ``fig_throughput``, ``fig_groups``, ``fig_phases``,
  ``fig_degradation``, ``fig_bounds``, ``fig_context`` (PDF for LaTeX, PNG for slides);
* ``tab_*.tex``: ``tabular`` bodies for the report (booktabs rules);
* ``*.csv``: the same tables in full precision, plus ``cut_short.csv``, the cycle each
  drive had in progress at the stop time (logged as a note, not counted), and
  ``cycle_failures.csv``, the failed cycles of each workload with the exact one-sided 95 %
  upper bound on the probability that a cycle fails;
* ``results.json``: the numbers quoted in the report text and on the slides.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from flashrel.analysis.degradation import normalised_paths  # noqa: E402
from flashrel.analysis.lifedata import cycles_frame, events_frame, life_table  # noqa: E402
from flashrel.analysis.parametric import (fit_lognormal_mle, fit_weibull_mle,  # noqa: E402
                                          weibayes_lower_bound)
from flashrel.analysis.planning import cycle_hours  # noqa: E402
from flashrel.config import Campaign, load_campaign  # noqa: E402
from flashrel.cycle import Outcome  # noqa: E402
from flashrel.failure import FAILURE_CODES  # noqa: E402
from flashrel.recorder import DriveLog  # noqa: E402
from flashrel.viz import style  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from planning_assumptions import NOMINAL  # noqa: E402
from results_figures import (_true, all_figures, cut_short_cycles, drive_order,  # noqa: E402
                             phase_table)

WORKLOADS = ("large", "medium", "small")
GROUPS = ("S8", "A8", "A16")
#: Sets of drives for the life data: all drives, each group, and the ABLAZE brand (A8 and
#: A16 report the same USB controller).
BRAND = ("ABLAZE", ("A8", "A16"))
#: Small counts are spelled out in running text.
NUMBER_WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                7: "seven", 8: "eight"}
SHAPES = (1.5, 2.0, 3.0)
LEVEL = 0.90
LOCAL_TZ = "America/New_York"
PHASES = {"write_mbps": "Write", "verify_mbps": "Read"}


# -- data ---------------------------------------------------------------------------------
def _local(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True).dt.tz_convert(LOCAL_TZ)


def _record(run_dir: Path, name: str) -> dict:
    path = run_dir / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}


def milestones(campaign: Campaign, cycles: pd.DataFrame) -> dict[str, pd.Timestamp | None]:
    """Pilot start, full start and stop time (local), from the supervisor records."""
    pilot = _record(campaign.run_dir, "supervisor-pilot.json").get("started")
    full = _record(campaign.run_dir, "supervisor-full.json").get("started")
    first = cycles["start"].min() if not cycles.empty else None
    stop = campaign.stop.end_time
    return {
        "pilot_start": pd.Timestamp(pilot).tz_convert(LOCAL_TZ) if pilot else first,
        "full_start": pd.Timestamp(full).tz_convert(LOCAL_TZ) if full else None,
        "stop": pd.Timestamp(stop).tz_localize(LOCAL_TZ) if stop else None,
    }


def load(campaign: Campaign) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    cycles = cycles_frame(campaign)
    if not cycles.empty:
        cycles = cycles.assign(start=_local(cycles["started_at"]), end=_local(cycles["ended_at"]),
                               path=np.where(cycles["port"].str.startswith("D"), "dock",
                                             "desktop"))
    return cycles, life_table(campaign), events_frame(campaign)


def usable_bytes(campaign: Campaign) -> dict[str, int]:
    out = {}
    for drive_id in campaign.assignments:
        log = DriveLog(campaign.run_dir, drive_id)
        intake = json.loads(log.intake_path.read_text()) if log.intake_path.exists() else {}
        cap = campaign.inventory[drive_id].capacity_gb * 1e9
        out[drive_id] = int(intake.get("total_bytes") or cap)
    return out


def lost_write_bytes(events: pd.DataFrame) -> dict[str, float]:
    """Bytes written in a cycle whose statistics were lost when its worker was stopped.

    The operator logs them in a note (``lost_write_bytes``, from the worker's I/O totals).
    They count toward the data written, not toward the data read back and compared.
    """
    col = "detail.lost_write_bytes"
    if events.empty or col not in events:
        return {}
    v = pd.to_numeric(events[col], errors="coerce")
    return v.groupby(events["drive_id"]).sum(min_count=1).dropna().to_dict()


# -- tables -------------------------------------------------------------------------------
def drive_summary(campaign: Campaign, cycles: pd.DataFrame, life: pd.DataFrame,
                  events: pd.DataFrame) -> pd.DataFrame:
    usable = usable_bytes(campaign)
    lost = lost_write_bytes(events)
    rows = []
    for drive_id, a in campaign.assignments.items():
        unit = campaign.inventory[drive_id]
        cyc = cycles[cycles["drive_id"] == drive_id] if not cycles.empty else cycles
        ok = cyc[cyc["outcome"] == "pass"] if len(cyc) else cyc
        lt = life[life["drive_id"] == drive_id].iloc[0]
        ev = events[events["drive_id"] == drive_id] if len(events) else events
        written = (float(cyc["write_bytes"].sum()) if len(cyc) else 0.0) + lost.get(drive_id, 0.0)
        bad = cyc[cyc["outcome"] != "pass"].sort_values("cycle") if len(cyc) else cyc
        row = {
            "failed_cycles": ", ".join(f"{int(c)} ({FAILURE_CODES[Outcome(o)]})"
                                       for c, o in zip(bad["cycle"], bad["outcome"])),
            "drive_id": drive_id, "group": unit.group.code, "brand": unit.brand,
            "capacity_gb": unit.capacity_gb, "port": a.port,
            "path": "dock" if a.port.startswith("D") else "desktop",
            "usable_bytes": usable[drive_id], "cycles": int(lt["cycles"]),
            "passed": int(len(ok)), "cycle_failures": int(len(cyc) - len(ok)),
            "hard_failed": bool(lt["hard_failed"]),
            "t_hard": int(lt["t_hard"]), "hard_code": lt["hard_code"], "status": lt["status"],
            "tb_written": written / 1e12,
            "tb_read": float(cyc["verify_bytes"].sum()) / 1e12 if len(cyc) else 0.0,
            "full_drive_writes": written / usable[drive_id],
            "transient_errors": int(cyc["transient_errors"].sum()) if len(cyc) else 0,
            "files_corrupt": int(cyc["files_corrupt"].sum()) if len(cyc) else 0,
            "files_missing": int(cyc["files_missing"].sum()) if len(cyc) else 0,
            "bits_corrupt": int(cyc["bits_corrupt"].sum()) if len(cyc) else 0,
            "slow_events": int((ev["type"] == "slow").sum()) if len(ev) else 0,
            "soft_failures": int(lt["soft_failures"]),
            "hours": float((cyc["end"].max() - cyc["start"].min()).total_seconds() / 3600)
            if len(cyc) else 0.0,
        }
        for w in WORKLOADS:
            sub = ok[ok["workload"] == w] if len(ok) else ok
            row[f"n_{w}"] = int(len(sub))
            row[f"write_{w}"] = float(sub["write_mbps"].median()) if len(sub) else math.nan
            row[f"read_{w}"] = float(sub["verify_mbps"].median()) if len(sub) else math.nan
            row[f"hours_{w}"] = float(sub["duration_s"].median() / 3600) if len(sub) else math.nan
        rows.append(row)
    return pd.DataFrame(rows)


def group_workload_summary(cycles: pd.DataFrame, drives: pd.DataFrame) -> pd.DataFrame:
    ok = cycles[cycles["outcome"] == "pass"]
    rows = []
    for g in GROUPS:
        for w in WORKLOADS:
            sub = ok[(ok["group"] == g) & (ok["workload"] == w)]
            per_drive = drives[drives["group"] == g][f"write_{w}"].dropna()
            per_drive_r = drives[drives["group"] == g][f"read_{w}"].dropna()
            rows.append({
                "group": g, "workload": w, "drives": int(sub["drive_id"].nunique()),
                "cycles": int(len(sub)),
                "write_median": float(sub["write_mbps"].median()) if len(sub) else math.nan,
                "write_drive_min": float(per_drive.min()) if len(per_drive) else math.nan,
                "write_drive_max": float(per_drive.max()) if len(per_drive) else math.nan,
                "read_median": float(sub["verify_mbps"].median()) if len(sub) else math.nan,
                "read_drive_min": float(per_drive_r.min()) if len(per_drive_r) else math.nan,
                "read_drive_max": float(per_drive_r.max()) if len(per_drive_r) else math.nan,
                "hours_median": float(sub["duration_s"].median() / 3600) if len(sub) else math.nan,
                "write_lat_p95_median_s": float(sub["write_lat_p95_s"].median())
                if len(sub) else math.nan,
                "files_median": float(sub["planned_files"].median()) if len(sub) else math.nan,
            })
    return pd.DataFrame(rows)


def bounds_table(life: pd.DataFrame, drives: pd.DataFrame) -> pd.DataFrame:
    """Weibayes lower bounds (or ML fits where at least two drives failed), per set."""
    started = life[life["cycles"] > 0].merge(drives[["drive_id", "full_drive_writes"]],
                                             on="drive_id")
    sets = _life_sets(started)
    rows = []
    for name, sub in sets:
        if sub.empty:
            continue
        t = sub["t_hard"].to_numpy(float)
        d = sub["hard_failed"].to_numpy(bool)
        fdw = sub["full_drive_writes"].to_numpy(float)  # includes bytes of a lost cycle
        row = {"set": name, "drives": int(len(t)), "failures": int(d.sum()),
               "cycles_on_test": float(t.sum()), "fdw_on_test": float(fdw.sum())}
        for s in SHAPES:
            row[f"theta_lower_g{s:g}"] = weibayes_lower_bound(t, d, s, LEVEL)
            row[f"fdw_lower_g{s:g}"] = weibayes_lower_bound(np.clip(fdw, 1e-9, None), d, s,
                                                            LEVEL)
        if d.sum() >= 2:  # the plan's maximum-likelihood fits, with 90 % Wald intervals
            try:
                fit = fit_weibull_mle(t, d)
                (slo, shi), (tlo, thi), (blo, bhi) = (fit.interval(q, LEVEL)
                                                     for q in ("shape", "scale", "B10"))
                row.update(mle_shape=fit.shape, mle_shape_lo=slo, mle_shape_hi=shi,
                           mle_scale=fit.scale, mle_scale_lo=tlo, mle_scale_hi=thi,
                           mle_mttf=fit.mttf, mle_b10=fit.quantile(0.10), mle_b10_lo=blo,
                           mle_b10_hi=bhi)
                logn = fit_lognormal_mle(t, d)
                row.update(logn_median=float(np.exp(logn.mu)), logn_sigma=logn.sigma,
                           logn_mttf=logn.mttf, loglik_weibull=fit.loglik,
                           loglik_lognormal=logn.loglik)
            except ValueError:
                pass
        rows.append(row)
    return pd.DataFrame(rows)


def _life_sets(started: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    sets = [("All drives", started)] + [(g, started[started["group"] == g]) for g in GROUPS]
    return sets + [(BRAND[0], started[started["group"].isin(BRAND[1])])]


def slope_table(cycles: pd.DataFrame, baseline_cycles: int) -> pd.DataFrame:
    """Per drive and workload: ln(throughput / baseline) = a + b k, with a 95 % interval on b."""
    rows = []
    ok = cycles[cycles["outcome"] == "pass"]
    for metric in PHASES:
        paths = normalised_paths(ok, metric=metric, baseline_cycles=baseline_cycles)
        for (drive, w), p in paths.groupby(["drive_id", "workload"], sort=True):
            k = p["cycle"].to_numpy(float)
            y = np.log(np.clip(p["relative"].to_numpy(float), 1e-6, None))
            row = {"metric": metric, "drive_id": drive, "group": p["group"].iloc[0],
                   "workload": w, "n": int(len(k)), "first_cycle": int(k.min()),
                   "last_cycle": int(k.max())}
            if len(k) >= 3 and np.ptp(k) > 0:
                fit = stats.linregress(k, y)
                half = stats.t.ppf(0.975, len(k) - 2) * fit.stderr
                lo, hi = fit.slope - half, fit.slope + half
                row.update(slope=fit.slope, slope_lo=lo, slope_hi=hi, intercept=fit.intercept,
                           pct_per_cycle=100 * math.expm1(fit.slope),
                           pct_lo=100 * math.expm1(lo), pct_hi=100 * math.expm1(hi),
                           significant_decline=bool(hi < 0),
                           k_star=(math.log(0.5) - fit.intercept) / fit.slope if hi < 0
                           else math.nan)
            rows.append(row)
    return pd.DataFrame(rows)


def measured_feasibility(campaign: Campaign, groups: pd.DataFrame,
                         hours_on_test: dict[str, float]) -> pd.DataFrame:
    """Planned against measured cycle times and cycles per day, per group."""
    rows = []
    for g in GROUPS:
        cap = campaign.inventory.groups[g].capacity_gb
        planned = {w: cycle_hours(cap, *NOMINAL[w]) for w in WORKLOADS}
        sub = groups[groups["group"] == g].set_index("workload")
        measured = {w: float(sub.loc[w, "hours_median"]) if w in sub.index else math.nan
                    for w in WORKLOADS}
        rot_p = sum(planned.values()) / 3
        rot_m = sum(measured.values()) / 3
        rows.append({"group": g, "capacity_gb": cap, **{f"planned_{w}": planned[w]
                                                        for w in WORKLOADS},
                     **{f"measured_{w}": measured[w] for w in WORKLOADS},
                     "planned_mean": rot_p, "measured_mean": rot_m,
                     "planned_cycles_24h": 24 / rot_p,
                     "measured_cycles_24h": 24 / rot_m if rot_m == rot_m else math.nan,
                     "hours_on_test": hours_on_test.get(g, math.nan)})
    return pd.DataFrame(rows)


# -- LaTeX --------------------------------------------------------------------------------
def _f(x, nd=1, dash="--"):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return dash
    return f"{x:,.{nd}f}".replace(",", "{,}")


def _sig(x, digits: int = 3, dash="--"):
    """``x`` to ``digits`` significant digits (534, 15.5, 0.310), for values spanning decades."""
    if x is None or not math.isfinite(x):
        return dash
    if x <= 0:
        return _f(x, 0)
    return _f(x, max(0, digits - 1 - math.floor(math.log10(x))))


def _in_order(frame: pd.DataFrame, order: list[str]) -> pd.DataFrame:
    """Rows of ``frame`` in the drive order of the figures; drives not in ``order`` last."""
    rank = {d: i for i, d in enumerate(order)}
    return frame.sort_values("drive_id", key=lambda s: s.map(lambda d: rank.get(d, len(rank))),
                             kind="stable")


def tex_drives(drives: pd.DataFrame, order: list[str]) -> str:
    lines = [r"\begin{tabular}{@{}llrrrrrrrrrl@{}}", r"\toprule",
             r" & & & & & \multicolumn{3}{c}{Write (MB/s)} & \multicolumn{3}{c}{Read (MB/s)} & \\",
             r"\cmidrule(lr){6-8}\cmidrule(lr){9-11}",
             r"Drive & Port & Cycles & TB & FDW & L & M & S & L & M & S & Outcome \\",
             r"\midrule"]
    for _, r in _in_order(drives, order).iterrows():
        soft = r["cycle_failures"]  # failed cycles other than the one that ended the drive
        if r["hard_failed"]:
            outcome = f"failed ({r['hard_code']}), cycle {r['t_hard']}"
            soft -= 1
        elif r["status"] == "attention":  # stopped by a failed recovery check
            outcome = f"stopped, cycle {r['cycles']}"
            soft -= 1
        else:  # running at the stop, or working again after the post-test check
            outcome = "censored"
        extra = []
        if soft > 0 and outcome == "censored":  # intermittent failures: which cycles
            extra.append(f"cycle {r['failed_cycles']} failed")
        elif soft > 0:
            extra.append(f"{soft} soft failure{'s' if soft > 1 else ''}")
        if r["transient_errors"]:
            extra.append(f"{r['transient_errors']} transient")
        if extra:
            outcome += "; " + ", ".join(extra)
        lines.append(
            f"{r['drive_id']} & {r['port']} & {r['cycles']} & {_f(r['tb_written'], 3)} & "
            f"{_f(r['full_drive_writes'], 1)} & "
            + " & ".join(_f(r[f"write_{w}"], 1) for w in WORKLOADS) + " & "
            + " & ".join(_f(r[f"read_{w}"], 1) for w in WORKLOADS)
            + f" & {outcome} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_groups(groups: pd.DataFrame) -> str:
    lines = [r"\begin{tabular}{@{}llrrrrr@{}}", r"\toprule",
             r" & & & Write & Read & Cycle & $p_{95}$ \\",
             r"Group & Workload & Cycles & (MB/s) & (MB/s) & (h) & (s) \\", r"\midrule"]
    for g in GROUPS:
        sub = groups[groups["group"] == g]
        for i, (_, r) in enumerate(sub.iterrows()):
            label = g if i == 0 else ""
            rng = ""
            if r["drives"] > 1 and math.isfinite(r["write_drive_min"]):
                rng = f" ({_f(r['write_drive_min'])}--{_f(r['write_drive_max'])})"
            lines.append(f"{label} & {r['workload']} & {r['cycles']} & "
                         f"{_f(r['write_median'])}{rng} & {_f(r['read_median'])} & "
                         f"{_f(r['hours_median'], 2)} & "
                         f"{_sig(r['write_lat_p95_median_s'])} \\\\")
        if g != GROUPS[-1]:
            lines.append(r"\addlinespace[2pt]")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_bounds(bounds: pd.DataFrame) -> str:
    lines = [r"\begin{tabular}{@{}lrrrrrrr@{}}", r"\toprule",
             r" & & & & \multicolumn{3}{c}{$\theta_{\mathrm{L}}$ (cycles)} & FDW \\",
             r"\cmidrule(lr){5-7}",
             r"Drives & $n$ & $r$ & Cycles & $\gamma=1.5$ & $\gamma=2$ & $\gamma=3$ & "
             r"$\gamma=2$ \\", r"\midrule"]
    for _, r in bounds.iterrows():
        lines.append(f"{r['set']} & {r['drives']} & {r['failures']} & {_f(r['cycles_on_test'], 0)} & "
                     f"{_f(r['theta_lower_g1.5'])} & {_f(r['theta_lower_g2'])} & "
                     f"{_f(r['theta_lower_g3'])} & {_f(r['fdw_lower_g2'])} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_mle(bounds: pd.DataFrame) -> str:
    """Maximum-likelihood fits, one column per set of drives with two or more failures."""
    fits = bounds[bounds["mle_shape"].notna()] if "mle_shape" in bounds else bounds.iloc[0:0]
    if fits.empty:
        return ("\\begin{tabular}{@{}l@{}}\\toprule\nNo set of drives has two failures, "
                "so no fit exists.\\\\\n\\bottomrule\n\\end{tabular}\n")

    def est(r, name, digits=2):
        return _sig(r[f"mle_{name}"], digits)

    def ci(r, name, digits=2):  # the interval goes on its own row, under the estimate
        return f"{_sig(r[f'mle_{name}_lo'], digits)}--{_sig(r[f'mle_{name}_hi'], digits)}"

    cols = list(fits.iterrows())
    within = r"\quad 90\,\% interval"
    rows = [("Drives, failures", [f"{r['drives']}, {r['failures']}" for _, r in cols]),
            (r"Shape $\hat\gamma$", [est(r, "shape") for _, r in cols]),
            (within, [ci(r, "shape") for _, r in cols]),
            (r"Scale $\hat\theta$", [est(r, "scale") for _, r in cols]),
            (within, [ci(r, "scale") for _, r in cols]),
            ("MTTF", [_sig(r["mle_mttf"], 2) for _, r in cols]),
            (r"$B_{10}$ life", [est(r, "b10") for _, r in cols]),
            (within, [ci(r, "b10") for _, r in cols]),
            ("Lognormal median", [_sig(r["logn_median"], 2) for _, r in cols])]
    lines = [f"\\begin{{tabular}}{{@{{}}l{'r' * len(cols)}@{{}}}}", r"\toprule",
             " & " + " & ".join(r["set"] for _, r in cols) + r" \\", r"\midrule"]
    lines += [f"{label} & " + " & ".join(cells) + r" \\" for label, cells in rows]
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_slopes(slopes: pd.DataFrame, order: list[str]) -> str:
    sub = slopes[slopes["metric"] == "write_mbps"]
    lines = [r"\begin{tabular}{@{}lrrr@{}}", r"\toprule",
             r"Drive & Large & Medium & Small \\", r"\midrule"]
    for drive in _in_order(sub, order)["drive_id"].unique():
        cells = []
        for w in WORKLOADS:
            r = sub[(sub["drive_id"] == drive) & (sub["workload"] == w)]
            if r.empty or not math.isfinite(r["pct_per_cycle"].iloc[0]):
                cells.append("--")
                continue
            r = r.iloc[0]
            mark = r"$^{\ast}$" if r["significant_decline"] else ""
            cells.append(f"{r['pct_per_cycle']:+.2f} [{r['pct_lo']:+.2f}, {r['pct_hi']:+.2f}]"
                         f"{mark}")
        lines.append(f"{drive} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_phases(phases: pd.DataFrame, order: list[str], workload: str = "small") -> str:
    """Time in each phase and file rates of one workload, one row per drive."""
    lines = [r"\begin{tabular}{@{}lrrrrrrr@{}}", r"\toprule",
             r" & & \multicolumn{4}{c}{Hours per cycle} & \multicolumn{2}{c}{Files per second} \\",
             r"\cmidrule(lr){3-6}\cmidrule(lr){7-8}",
             r"Drive & Files & Write & Read & Delete & Total & Write & Delete \\", r"\midrule"]
    sub = phases[phases["workload"] == workload]
    for d in order:
        r = sub[sub["drive_id"] == d]
        if r.empty:
            lines.append(f"{d} & \\multicolumn{{7}}{{l}}{{no {workload}-file cycle completed}} \\\\")
            continue
        r = r.iloc[0]
        if not r.get("logged", True):  # failed, and its phase times died with the worker
            lines.append(f"{d} & \\multicolumn{{7}}{{l}}{{cycle {int(r['cycle'])} failed; "
                         "phase times not logged} \\\\")
            continue
        rate = r["delete_files_per_s"]
        mark = "" if r["outcome"] == "pass" else r"$^{\dagger}$"  # failed while deleting
        lines.append(f"{d}{mark} & {_f(r['files'], 0)} & {_f(r['write_h'], 2)} & "
                     f"{_f(r['verify_h'], 2)} & {_f(r['delete_h'], 2)} & {_f(r['total_h'], 2)} & "
                     f"{_f(r['write_files_per_s'], 1)} & {_f(rate, 0 if rate >= 100 else 1)} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def tex_pilot(cycles: pd.DataFrame, marks: dict, order: list[str]) -> str:
    """The cycles that ended before the other drives started, i.e. those of the pilot."""
    full = marks.get("full_start")
    sub = cycles if full is None else cycles[cycles["end"] <= full]
    lines = [r"\begin{tabular}{@{}lrlrrrr@{}}", r"\toprule",
             r" & & & & Time & Write & Read \\",
             r"Drive & Cycle & Workload & Files & (min) & (MB/s) & (MB/s) \\", r"\midrule"]
    for _, r in _in_order(sub.sort_values("cycle"), order).iterrows():
        work = r["workload"] if r["outcome"] == "pass" else f"{r['workload']} ({r['outcome']})"
        lines.append(f"{r['drive_id']} & {r['cycle']} & {work} & {_f(r['planned_files'], 0)} & "
                     f"{_f(r['duration_s'] / 60, 1)} & {_f(r['write_mbps'], 2)} & "
                     f"{_f(r['verify_mbps'], 2)} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def km_steps(life: pd.DataFrame) -> pd.DataFrame:
    """Kaplan-Meier steps of hard failures, for all drives pooled and for each group."""
    started = life[life["cycles"] > 0]
    rows = []
    for name, sub in _life_sets(started):
        t = sub["t_hard"].to_numpy(float)
        d = sub["hard_failed"].to_numpy(bool)
        r = 1.0
        for tf in sorted(set(t[d])):
            n, k = int((t >= tf).sum()), int(((t == tf) & d).sum())
            r *= 1 - k / n
            rows.append({"set": name, "cycle": int(tf), "at_risk": n, "failures": k,
                         "reliability": r})
    return pd.DataFrame(rows, columns=["set", "cycle", "at_risk", "failures", "reliability"])


def failure_splits(cycles: pd.DataFrame, life: pd.DataFrame) -> dict[str, dict]:
    """Where did the failures fall, against each part's share of the exposure?

    If every cycle had the same chance to fail, the failures that fall in a part with a
    share ``s`` of the cycles are binomial(r, s); the one-sided p-value is the chance of
    at least as many as observed. ``brand``: hard failures on ABLAZE drives, exposure in
    cycles on test; ``brandcycles``: failed cycles on ABLAZE drives among all failed cycles;
    ``small``: failed cycles among the small-file cycles.
    """
    started = life[life["cycles"] > 0]
    ablaze = started["group"].isin(BRAND[1])
    failed = cycles["outcome"] != "pass"
    on_ablaze = cycles["group"].isin(BRAND[1])
    parts = {
        "brand": (int(started["hard_failed"].sum()), int(started.loc[ablaze, "hard_failed"].sum()),
                  float(started.loc[ablaze, "t_hard"].sum() / started["t_hard"].sum())),
        "brandcycles": (int(failed.sum()), int((failed & on_ablaze).sum()),
                        float(on_ablaze.mean())),
        "small": (int(failed.sum()), int((failed & (cycles["workload"] == "small")).sum()),
                  float((cycles["workload"] == "small").mean())),
    }
    return {k: {"failures": r, "in_part": x, "share": s,
                "p_value": float(stats.binom.sf(x - 1, r, s)) if r else math.nan}
            for k, (r, x, s) in parts.items()}


def failure_bounds(cycles: pd.DataFrame, level: float = 0.95) -> pd.DataFrame:
    """Per workload: failed cycles and the exact one-sided upper bound on the failure probability."""
    rows = []
    for w in WORKLOADS:
        sub = cycles[cycles["workload"] == w]
        n, x = int(len(sub)), int((sub["outcome"] != "pass").sum())
        upper = float(stats.beta.ppf(level, x + 1, n - x)) if n > x else 1.0
        rows.append({"workload": w, "cycles": n, "failed": x,
                     "p_hat": x / n if n else math.nan, "p_upper": upper if n else math.nan})
    return pd.DataFrame(rows)


def tex_feasibility(feas: pd.DataFrame) -> str:
    lines = [r"\begin{tabular}{@{}lrrrrr@{}}", r"\toprule",
             r" & \multicolumn{3}{c}{Hours per cycle, planned / measured} & "
             r"\multicolumn{2}{c}{Cycles in 24 h} \\", r"\cmidrule(lr){2-4}\cmidrule(lr){5-6}",
             r"Group & Large & Medium & Small & Planned & Measured \\", r"\midrule"]
    for _, r in feas.iterrows():
        cells = [f"{_f(r[f'planned_{w}'], 2)} / {_f(r[f'measured_{w}'], 2)}" for w in WORKLOADS]
        lines.append(f"{r['group']} & " + " & ".join(cells)
                     + f" & {_f(r['planned_cycles_24h'], 0)} & {_f(r['measured_cycles_24h'], 1)}"
                     + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines) + "\n"


def _num(x, nd: int = 1) -> str:
    """A number for running text: thousands separators, fixed decimals, '--' if missing."""
    return _f(x, nd)


def _sci(x: float | None) -> str:
    if x is None or not math.isfinite(x) or x <= 0:
        return "--"
    exp = math.floor(math.log10(x))
    return f"{x / 10 ** exp:.1f}\\times10^{{{exp}}}"


def tex_numbers(numbers: dict, drives: pd.DataFrame, groups: pd.DataFrame,
                bounds: pd.DataFrame, feas: pd.DataFrame, slopes: pd.DataFrame,
                phases: pd.DataFrame, fails: pd.DataFrame, km: pd.DataFrame,
                cycles: pd.DataFrame, splits: dict[str, dict]) -> str:
    """``\\res{key}`` macros for every number the report text quotes."""
    vals: dict[str, str] = {
        "cut-short": _num(len(numbers.get("cut_short") or []), 0),
        "cycles-total": _num(numbers["cycles_total"], 0),
        "cycles-passed": _num(numbers["cycles_passed"], 0),
        "cycle-failures": _num(numbers["cycle_failures"], 0),
        "hard-failures": _num(numbers["hard_failures"], 0),
        "tb-written": _num(numbers["tb_written"], 2),
        "tb-read": _num(numbers["tb_read"], 2),
        "files-written": _num(numbers["files_written"], 0),
        "files-corrupt": _num(numbers["files_corrupt"], 0),
        "bits-corrupt": _num(numbers["bits_corrupt"], 0),
        "bits-read": _sci(float(numbers["bits_read"] or 0)),
        "ber-upper": _sci(numbers.get("ber_upper_95")),
        "transient": _num(numbers["transient_errors"], 0),
        "slow": _num(numbers["slow_events"], 0),
        "declines": _num(len(numbers.get("significant_declines") or []), 0),
    }
    for _, b in bounds.iterrows():
        key = "pooled" if b["set"] == "All drives" else b["set"]
        for s in SHAPES:
            vals[f"theta-{key}-g{s:g}"] = _num(b[f"theta_lower_g{s:g}"], 0)
            vals[f"fdw-{key}-g{s:g}"] = _num(b[f"fdw_lower_g{s:g}"], 0)
        vals[f"cycles-on-test-{key}"] = _num(b["cycles_on_test"], 0)
        vals[f"failures-{key}"] = _num(b["failures"], 0)
        vals[f"drives-{key}"] = _num(b["drives"], 0)
        if pd.notna(b.get("mle_shape")):
            for q in ("shape", "scale", "b10"):
                for suffix in ("", "_lo", "_hi"):
                    vals[f"mle-{key}-{q}{suffix.replace('_', '-')}"] = _sig(b[f"mle_{q}{suffix}"], 2)
            vals[f"mle-{key}-mttf"] = _sig(b["mle_mttf"], 2)
            vals[f"logn-{key}-median"] = _sig(b["logn_median"], 2)
    planned = [float(feas.loc[feas["group"] == g, "planned_cycles_24h"].iloc[0])
               for g in drives["group"]]
    for s in SHAPES:
        vals[f"theta-planned-g{s:g}"] = _num(weibayes_lower_bound(planned, [False] * len(planned),
                                                                  s, LEVEL), 0)
    for _, d in drives.iterrows():
        k = d["drive_id"]
        vals[f"{k}-cycles"] = _num(d["cycles"], 0)
        vals[f"{k}-tb"] = _num(d["tb_written"], 3)
        vals[f"{k}-fdw"] = _sig(d["full_drive_writes"], 2)
        vals[f"{k}-transient"] = _num(d["transient_errors"], 0)
        for w in WORKLOADS:
            vals[f"{k}-{w}-write"] = _num(d[f"write_{w}"], 1)
            vals[f"{k}-{w}-read"] = _num(d[f"read_{w}"], 1)
    for _, p in phases.iterrows():
        if not p.get("logged", True):  # no phase times to report
            continue
        k = f"{p['drive_id']}-{p['workload']}"
        for name in ("write", "verify", "delete", "total"):
            vals[f"{k}-{name}-h"] = _num(p[f"{name}_h"], 2)
        vals[f"{k}-files"] = _num(p["files"], 0)
        vals[f"{k}-write-fps"] = _num(p["write_files_per_s"], 1)
        rate = p["delete_files_per_s"]
        vals[f"{k}-delete-fps"] = _num(rate, 0 if rate >= 100 else 1)
    for _, r in groups.iterrows():
        k = f"{r['group']}-{r['workload']}"
        vals[f"{k}-write"] = _num(r["write_median"], 1)
        vals[f"{k}-read"] = _num(r["read_median"], 1)
        vals[f"{k}-hours"] = _num(r["hours_median"], 2)
        vals[f"{k}-files"] = _num(r["files_median"], 0)
        vals[f"{k}-p95ms"] = _num(1000 * r["write_lat_p95_median_s"], 0)
        vals[f"{k}-p95s"] = _sig(r["write_lat_p95_median_s"])
        vals[f"{k}-cycles"] = _num(r["cycles"], 0)
    for _, f in feas.iterrows():
        g = f["group"]
        vals[f"{g}-planned-h"] = _num(f["planned_mean"], 2)
        vals[f"{g}-measured-h"] = _num(f["measured_mean"], 2)
        vals[f"{g}-planned-cycles"] = _num(f["planned_cycles_24h"], 0)
        vals[f"{g}-measured-cycles"] = _num(f["measured_cycles_24h"], 1)
    # Files that could not be found at read-back, and the data they held (bytes written
    # minus bytes read back in those cycles).
    lost = cycles[cycles["files_missing"] > 0]
    vals["files-missing"] = _num(int(cycles["files_missing"].sum()), 0)
    vals["missing-mb"] = _num(float((lost["write_bytes"] - lost["verify_bytes"]).sum()) / 1e6, 1)
    for _, k in km.groupby("set").tail(1).iterrows():  # the last step of each set
        key = "pooled" if k["set"] == "All drives" else k["set"]
        vals[f"km-{key}-cycle"] = _num(k["cycle"], 0)
        vals[f"km-{key}-atrisk"] = _num(k["at_risk"], 0)
        vals[f"km-{key}-R"] = _num(k["reliability"], 2)
    for k, sp in splits.items():
        vals[f"split-{k}-share"] = _num(100 * sp["share"], 0)
        vals[f"split-{k}-p"] = _sig(sp["p_value"], 2)  # 0.088, 0.015, 0.0029
    vals["drive-cycles-min"] = _num(drives["cycles"].min(), 0)
    vals["drive-cycles-max"] = _num(drives["cycles"].max(), 0)
    vals["drive-fdw-min"] = _sig(drives["full_drive_writes"].min(), 2)
    vals["drive-fdw-max"] = _sig(drives["full_drive_writes"].max(), 2)
    survived = drives[~drives["hard_failed"].astype(bool)]  # right-censored at the stop
    vals["censored-drives"] = NUMBER_WORDS.get(len(survived), _num(len(survived), 0))
    vals["censored-cycles-min"] = _num(survived["cycles"].min(), 0)
    vals["censored-cycles-max"] = _num(survived["cycles"].max(), 0)
    vals["censored-fdw-min"] = _sig(survived["full_drive_writes"].min(), 2)
    vals["censored-fdw-max"] = _sig(survived["full_drive_writes"].max(), 2)
    for _, f in fails.iterrows():
        vals[f"cycles-{f['workload']}"] = _num(f["cycles"], 0)
        vals[f"failed-{f['workload']}"] = _num(f["failed"], 0)
        vals[f"pfail-upper-{f['workload']}"] = _num(100 * f["p_upper"], 0)
    for g in GROUPS:
        sub = drives[drives["group"] == g]
        vals[f"{g}-cycles-min"] = _num(sub["cycles"].min(), 0)
        vals[f"{g}-cycles-max"] = _num(sub["cycles"].max(), 0)
        vals[f"{g}-fdw-min"] = _sig(sub["full_drive_writes"].min(), 2)
        vals[f"{g}-fdw-max"] = _sig(sub["full_drive_writes"].max(), 2)
    lines = ["% generated by Project_code/scripts/make_results.py; do not edit",
             r"\makeatletter",
             r"\newcommand{\resdef}[2]{\expandafter\def\csname res@#1\endcsname{#2}}",
             r"\newcommand{\res}[1]{\@ifundefined{res@#1}{\textbf{??#1??}}"
             r"{\csname res@#1\endcsname}}",
             r"\makeatother"]
    lines += [f"\\resdef{{{k}}}{{{v}}}" for k, v in sorted(vals.items())]
    return "\n".join(lines) + "\n"


# -- key numbers --------------------------------------------------------------------------
def key_numbers(campaign: Campaign, cycles: pd.DataFrame, drives: pd.DataFrame,
                groups: pd.DataFrame, bounds: pd.DataFrame, slopes: pd.DataFrame,
                events: pd.DataFrame, marks: dict, feas: pd.DataFrame,
                phases: pd.DataFrame, cut: pd.DataFrame) -> dict:
    ok = cycles[cycles["outcome"] == "pass"]
    pooled = bounds[bounds["set"] == "All drives"].iloc[0].to_dict() if len(bounds) else {}
    if len(slopes) and "significant_decline" in slopes:
        sig = slopes[(slopes["metric"] == "write_mbps")
                     & slopes["significant_decline"].map(_true).astype(bool)]
    else:
        sig = slopes
    ev_counts = events["type"].value_counts().to_dict() if len(events) else {}

    def iso(t):
        return t.isoformat() if t is not None else None

    # Every byte read back was compared, so the bits read bound the bit error ratio of the
    # read path: with b corrupted bits in n, the one-sided 95 % upper bound (exact binomial,
    # ~3/n for b = 0, the "rule of three").
    bits_read = int(cycles["verify_bytes"].sum() * 8) if len(cycles) else 0
    bits_bad = int(drives["bits_corrupt"].sum())
    ber_upper = float(stats.beta.ppf(0.95, bits_bad + 1, max(bits_read - bits_bad, 1))) \
        if bits_read else None

    return {
        "campaign": campaign.name,
        "pilot_start": iso(marks["pilot_start"]), "full_start": iso(marks["full_start"]),
        "stop": iso(marks["stop"]),
        "last_cycle_end": iso(cycles["end"].max()) if len(cycles) else None,
        "drives": int(len(drives)),
        "cycles_total": int(len(cycles)), "cycles_passed": int(len(ok)),
        "cycle_failures": int(len(cycles) - len(ok)),
        "hard_failures": int(drives["hard_failed"].sum()),
        "tb_written": float(drives["tb_written"].sum()),
        "tb_read": float(drives["tb_read"].sum()),
        "files_written": int(cycles["planned_files"].sum()) if len(cycles) else 0,
        "files_corrupt": int(drives["files_corrupt"].sum()),
        "bits_corrupt": bits_bad,
        "bits_read": bits_read,
        "ber_upper_95": ber_upper,
        "transient_errors": int(drives["transient_errors"].sum()),
        "slow_events": int(drives["slow_events"].sum()),
        "event_counts": {str(k): int(v) for k, v in ev_counts.items()},
        "cache_bypassed_all": bool(cycles["cache_bypassed"].astype(int).eq(1).all())
        if len(cycles) else None,
        "per_drive": drives.replace({np.nan: None}).to_dict(orient="records"),
        "per_group_workload": groups.replace({np.nan: None}).to_dict(orient="records"),
        "bounds": bounds.replace({np.nan: None}).to_dict(orient="records"),
        "pooled_theta_lower_g2": pooled.get("theta_lower_g2"),
        "significant_declines": sig[["drive_id", "workload", "pct_per_cycle", "pct_lo",
                                     "pct_hi"]].to_dict(orient="records") if len(sig) else [],
        "feasibility": feas.replace({np.nan: None}).to_dict(orient="records"),
        "phases": phases.replace({np.nan: None}).to_dict(orient="records"),
        "cut_short": cut.assign(start=cut["start"].map(iso), end=cut["end"].map(iso))
        .replace({np.nan: None}).to_dict(orient="records") if len(cut) else [],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    campaign = load_campaign(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    style.apply()

    cycles, life, events = load(campaign)
    if cycles.empty:
        print("no cycles logged yet")
        return 1
    marks = milestones(campaign, cycles)
    drives = drive_summary(campaign, cycles, life, events)
    groups = group_workload_summary(cycles, drives)
    bounds = bounds_table(life, drives)
    slopes = slope_table(cycles, campaign.failure.baseline_cycles)
    hours = drives.groupby("group")["hours"].median().to_dict()
    feas = measured_feasibility(campaign, groups, hours)
    phases = phase_table(cycles)
    cut = cut_short_cycles(cycles, events, marks)
    fails = failure_bounds(cycles)
    km = km_steps(life)
    splits = failure_splits(cycles, life)

    for name, frame in (("drives", drives), ("groups", groups), ("bounds", bounds),
                        ("slopes", slopes), ("feasibility", feas), ("phases", phases),
                        ("cut_short", cut), ("cycle_failures", fails), ("km_steps", km)):
        frame.to_csv(out / f"{name}.csv", index=False)
    order = drive_order(campaign)
    for name, text in (("tab_results_drives", tex_drives(drives, order)),
                       ("tab_results_groups", tex_groups(groups)),
                       ("tab_bounds", tex_bounds(bounds)),
                       ("tab_slopes", tex_slopes(slopes, order)),
                       ("tab_feasibility_measured", tex_feasibility(feas)),
                       ("tab_phases", tex_phases(phases, order)),
                       ("tab_pilot", tex_pilot(cycles, marks, order)),
                       ("tab_mle", tex_mle(bounds))):
        (out / f"{name}.tex").write_text(
            "% generated by Project_code/scripts/make_results.py; do not edit\n" + text,
            encoding="utf-8")
    planned = {g: float(feas.loc[feas["group"] == g, "planned_cycles_24h"].iloc[0])
               for g in feas["group"]}
    written = all_figures(campaign, cycles, life, drives, slopes, marks, planned, out, events)
    paths = normalised_paths(cycles[cycles["outcome"] == "pass"], metric="write_mbps",
                             baseline_cycles=campaign.failure.baseline_cycles)
    (out / "paths.json").write_text(json.dumps({
        d: {"group": str(p["group"].iloc[0]), "dock": bool((p["path"] == "dock").iloc[0]),
            "cycle": p.sort_values("cycle")["cycle"].astype(int).tolist(),
            "relative": p.sort_values("cycle")["relative"].round(4).tolist()}
        for d, p in paths.groupby("drive_id")}, indent=1), encoding="utf-8")
    numbers = key_numbers(campaign, cycles, drives, groups, bounds, slopes, events, marks, feas,
                          phases, cut)
    numbers["cycle_failure_bounds"] = fails.to_dict(orient="records")
    numbers["km_steps"] = km.to_dict(orient="records")
    numbers["failure_splits"] = splits
    (out / "results.json").write_text(json.dumps(numbers, indent=2, default=str),
                                      encoding="utf-8")
    (out / "numbers.tex").write_text(tex_numbers(numbers, drives, groups, bounds, feas, slopes,
                                                 phases, fails, km, cycles, splits),
                                     encoding="utf-8")
    print(f"{numbers['cycles_total']} cycles, {numbers['tb_written']:.3f} TB written, "
          f"{numbers['hard_failures']} hard failures; wrote {len(written)} figure files to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
