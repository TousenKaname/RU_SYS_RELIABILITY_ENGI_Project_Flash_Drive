"""Figures of the final report (called by ``make_results.py``).

They share the visual system of the test plan (``flashrel.viz.style``): 7 pt Arial,
thin axes, hairline gridlines, brand as hue and capacity as lightness, vermilion only
for failures and thresholds. Marks follow one recipe: 1 pt lines, filled markers with
a white ring for desktop ports and open markers for the dock, a legend for every
figure with more than one series, and direct labels only where they carry the story.
Each figure is saved at the exact IEEE column or page width, so LaTeX places it 1:1.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, LogLocator, MaxNLocator, NullFormatter

from flashrel.analysis.degradation import normalised_paths
from flashrel.analysis.parametric import weibayes_lower_bound
from flashrel.config import Campaign
from flashrel.viz import diagrams, style

WORKLOADS = ("large", "medium", "small")
GROUPS = ("S8", "A8", "A16")
SIZES = {"large": "512 MiB to 2 GiB", "medium": "1 to 64 MiB", "small": "4 to 256 KiB"}
PHASES = {"write_mbps": "Write", "verify_mbps": "Read"}
BAND = "#F0F2F5"        # light surface band (pilot period, earlier failures)
LEVEL = 0.90


# -- shared pieces ------------------------------------------------------------------------
def drive_order(campaign: Campaign) -> list[str]:
    """Drives grouped by lot (S8, A8, A16), each group in unit-ID order."""
    return [d for g in GROUPS for d in sorted(campaign.assignments)
            if campaign.inventory[d].group.code == g]


def _grid(ax, axis: str = "y") -> None:
    ax.grid(axis=axis, which="major", color=style.GRID, lw=0.5, zorder=0)
    ax.set_axisbelow(True)


def _marker(group: str, dock: bool, size: float = 3.8) -> dict:
    """Filled marker with a white ring (desktop port) or an open marker (dock)."""
    color = style.GROUP_COLORS[group]
    if dock:
        return dict(marker=style.GROUP_MARKERS[group], ms=size, mfc="white", mec=color,
                    mew=0.9)
    return dict(marker=style.GROUP_MARKERS[group], ms=size, mfc=color, mec="white", mew=0.6)


def _group_handles(dock_note: bool = True, line: bool = True) -> list:
    handles = [Line2D([], [], color=style.GROUP_COLORS[g], lw=1.0 if line else 0,
                      label=style.GROUP_LABELS[g], **_marker(g, False)) for g in GROUPS]
    if dock_note:
        handles.append(Line2D([], [], color=style.GRAY, lw=1.0 if line else 0, marker="o",
                              ms=3.8, mfc="white", mec=style.GRAY, mew=0.9,
                              label="on the dock (open)"))
    return handles


def _nice_axis(ax, peak: float, n: int = 4, axis: str = "y") -> None:
    """Linear axis from 0 to the first round tick at or above ``peak``, so it ends on a tick."""
    raw = max(peak, 1e-9) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = step * math.ceil(peak / step - 1e-9)
    ticks = np.arange(0, top + step / 2, step)
    if axis == "y":
        ax.set_ylim(0, top)
        ax.set_yticks(ticks)
    else:
        ax.set_xlim(0, top)
        ax.set_xticks(ticks)


def _log_axis(ax, axis: str = "y") -> None:
    target = ax.yaxis if axis == "y" else ax.xaxis
    (ax.set_yscale if axis == "y" else ax.set_xscale)("log")
    target.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    target.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.6g}"))
    target.set_minor_formatter(NullFormatter())


def _naive(t: pd.Timestamp | None):
    if t is None:
        return None
    return t.tz_localize(None) if t.tzinfo is not None else t


def _true(value) -> bool:
    """True for a real boolean True; False for False, None and NaN (object columns)."""
    return isinstance(value, (bool, np.bool_)) and bool(value)


CUT_SHORT = re.compile(r"stop time reached during cycle (\d+)")


def cut_short_cycles(cycles: pd.DataFrame, events: pd.DataFrame, marks: dict) -> pd.DataFrame:
    """The cycle each drive had in progress at the stop time (logged as a note, not counted).

    It started when the drive's last counted cycle ended and ended at the stop.
    """
    cols = ["drive_id", "cycle", "workload", "start", "end", "write_bytes", "verify_bytes",
            "files_corrupt", "files_missing", "transient_errors"]
    if events is None or events.empty or "detail.text" not in events:
        return pd.DataFrame(columns=cols)
    notes = events[(events["type"] == "note")
                   & events["detail.text"].fillna("").str.match(CUT_SHORT)]
    rows = []
    for _, e in notes.iterrows():
        d = e["drive_id"]
        done = cycles[cycles["drive_id"] == d]
        start = done["end"].max() if len(done) else marks.get("full_start")
        end = pd.Timestamp(e["time"])
        end = end.tz_convert(start.tzinfo) if end.tzinfo is not None and start is not None else end
        rows.append({"drive_id": d, "cycle": int(CUT_SHORT.match(e["detail.text"]).group(1)),
                     "workload": e.get("detail.workload"), "start": start, "end": end,
                     **{k: e.get(f"detail.{k}") for k in cols[5:]}})
    return pd.DataFrame(rows, columns=cols)


# -- Fig. timeline -------------------------------------------------------------------------
def fig_timeline(campaign: Campaign, cycles: pd.DataFrame, life: pd.DataFrame, marks: dict,
                 out: Path, cut: pd.DataFrame | None = None, size: tuple | None = None,
                 name: str = "fig_timeline", legend_cols: int | None = None) -> list[Path]:
    """Every cycle of every drive as a bar from its start to its end, in clock time.

    ``size`` (inches) draws a smaller copy with the same type, for a slide.
    """
    fig_w, fig_h = size or (style.PAGE_W, 2.9)
    stretch = style.PAGE_W / fig_w  # hours per inch, relative to the page-width figure
    order = drive_order(campaign)
    rows, y = {}, 0.0
    for g in GROUPS:
        for d in order:
            if campaign.inventory[d].group.code == g:
                rows[d] = y
                y += 1.0
        y += 0.45
    t_pilot, t_full, t_stop = (_naive(marks.get(k)) for k in ("pilot_start", "full_start", "stop"))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    top = -0.85
    if t_pilot is not None and t_full is not None:
        ax.axvspan(t_pilot, t_full, color=BAND, lw=0, zorder=0)
        ax.text(t_pilot + pd.Timedelta(minutes=6), top, "pilot", ha="left", va="bottom",
                fontsize=6, color=style.GRAY)
    cut = cut if cut is not None else pd.DataFrame(columns=["drive_id"])
    row_end: dict[str, pd.Timestamp] = {}
    for _, c in cut.iterrows():  # the cycle in progress at the stop: outlined, not counted
        if c["drive_id"] not in rows or c["start"] is None:
            continue
        s, e = _naive(pd.Timestamp(c["start"])), _naive(pd.Timestamp(c["end"]))
        color = style.WORKLOAD_COLORS.get(c["workload"], style.MIST)
        with plt.rc_context({"hatch.linewidth": 0.5}):
            ax.barh(rows[c["drive_id"]], mdates.date2num(e) - mdates.date2num(s),
                    left=mdates.date2num(s), height=0.64, facecolor="white", edgecolor=color,
                    hatchcolor=color, hatch="//////", linewidth=0.6, zorder=2)
        row_end[c["drive_id"]] = e
    any_failed = any_transient = False
    last_end = t_pilot
    for d in order:
        cyc = cycles[cycles["drive_id"] == d].sort_values("cycle")
        if cyc.empty:
            continue
        starts = cyc["start"].dt.tz_localize(None)
        ends = cyc["end"].dt.tz_localize(None)
        # a cycle logged without phases (its worker was restarted and found no drive) ran
        # from the end of the drive's previous cycle, since the workers cycle back to back
        prev_end = ends.shift(1)
        starts = starts.where(~((ends == starts) & prev_end.notna()), prev_end)
        left = mdates.date2num(starts.to_numpy())
        width = mdates.date2num(ends.to_numpy()) - left
        colors = [style.WORKLOAD_COLORS[w] if o == "pass" else style.FAIL
                  for w, o in zip(cyc["workload"], cyc["outcome"])]
        any_failed |= bool((cyc["outcome"] != "pass").any())
        ax.barh(np.full(len(cyc), rows[d]), width, left=left, height=0.64, color=colors,
                edgecolor="white", linewidth=0.8, zorder=2)
        noisy = cyc[cyc["transient_errors"] > 0]
        if len(noisy):
            any_transient = True
            ax.plot(noisy["end"].dt.tz_localize(None), np.full(len(noisy), rows[d] - 0.47),
                    ls="none", marker="v", ms=3.2, mfc=style.INK, mec="white", mew=0.4,
                    zorder=4)
        end = max(ends.max(), row_end.get(d, ends.max()))
        last_end = max(last_end, end)
        lt = life.loc[life["drive_id"] == d].iloc[0]
        n = int(lt["cycles"])
        if _true(lt["hard_failed"]):
            label = f"failed in cycle {int(lt['t_hard'])}"
        elif lt["status"] == "attention":  # stopped by a failed recovery check
            label = f"cycle {n} failed; stopped"
        elif cyc["outcome"].iloc[-1] != "pass":  # stopped, then worked again after the test
            label = f"cycle {n} failed; intermittent"
        else:
            label = f"{n} cycles"
        # A label that would run into the stop line goes inside the drive's last bar instead
        # (about 0.19 h per character at the page width and this font size).
        label_h = pd.Timedelta(hours=0.19 * stretch * len(label) + 0.2)
        last_w = ends.iloc[-1] - starts.iloc[-1]
        if (t_stop is not None and end < t_stop < end + label_h
                and end == ends.iloc[-1] and last_w > label_h + pd.Timedelta(hours=0.4)):
            ax.text(end - pd.Timedelta(minutes=12), rows[d], label, va="center",
                    ha="right", fontsize=6, color="white", zorder=4)
        else:
            ax.text(end + pd.Timedelta(minutes=12), rows[d], label, va="center",
                    ha="left", fontsize=6, color=style.GRAY)
    for t, label in ((t_full, "other six start"), (t_stop, "stop")):
        if t is None:
            continue
        ax.axvline(t, color=style.INK, lw=0.6, zorder=3)
        ax.text(t + pd.Timedelta(minutes=8), top, f"{label}, {t:%a %H:%M}", ha="left",
                va="bottom", fontsize=6, color=style.INK)
    ax.set_yticks(list(rows.values()))
    ax.set_yticklabels([f"{d}  {campaign.assignments[d].port}" for d in rows], fontsize=6.5)
    ax.tick_params(axis="y", length=0, pad=10)
    for d, yy in rows.items():
        g = campaign.inventory[d].group.code
        ax.plot([-0.008], [yy], transform=ax.get_yaxis_transform(), marker="s", ms=4.0,
                color=style.GROUP_COLORS[g], clip_on=False, zorder=5)
    ax.set_ylim(y - 0.45 + 0.05, top - 0.25)
    right = max(last_end, t_stop or last_end) + pd.Timedelta(hours=1.6 * stretch)
    ax.set_xlim(t_pilot - pd.Timedelta(minutes=15), right)
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    for day in pd.date_range(t_pilot.normalize(), right, freq="D"):
        x = max(day, t_pilot)
        ax.annotate(f"{x:%a %d %b}", xy=(mdates.date2num(x), 0), xycoords=("data", "axes fraction"),
                    xytext=(0, -17), textcoords="offset points", fontsize=6, color=style.GRAY,
                    ha="left")
    for side in ("left", "top", "right"):
        ax.spines[side].set_visible(False)
    _grid(ax, "x")
    handles = [Patch(color=style.WORKLOAD_COLORS[w], label=f"{w} files") for w in WORKLOADS]
    if len(cut):
        handles.append(Patch(facecolor="white", edgecolor=style.GRAY, hatch="//////",
                             linewidth=0.6, label="cut short at the stop (not counted)"))
    if any_failed:  # absent kinds stay out of the legend; the caption says they did not occur
        handles.append(Patch(color=style.FAIL, label="failed cycle"))
    if any_transient:
        handles.append(Line2D([], [], ls="none", marker="v", ms=3.2, mfc=style.INK, mec="white",
                              label="transient error"))
    handles += [Line2D([], [], ls="none", marker="s", ms=4.0, color=style.GROUP_COLORS[g],
                       label=style.GROUP_LABELS[g]) for g in GROUPS]
    with plt.rc_context({"hatch.linewidth": 0.5}):
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.0, 1.03),
                  ncol=legend_cols or len(handles), fontsize=6, handlelength=1.2,
                  columnspacing=1.1, borderaxespad=0)
        return style.save(fig, out / name, width=fig_w)


# -- Fig. throughput -----------------------------------------------------------------------
def fig_throughput(campaign: Campaign, cycles: pd.DataFrame, out: Path) -> list[Path]:
    """Write and read throughput of every passing cycle, one panel per workload and phase."""
    ok = cycles[cycles["outcome"] == "pass"]
    order = drive_order(campaign)
    fig, axes = plt.subplots(2, 3, figsize=(style.PAGE_W, 3.5), sharex="col")
    # one scale per quantity: write throughput of large and medium files share one axis (small
    # files, ten times slower, keep their own), and every read panel shares one axis
    peak = {(m, w): float(ok.loc[ok["workload"] == w, m].max()) for m in PHASES for w in WORKLOADS}
    tops = {("write_mbps", "large"): max(peak["write_mbps", "large"], peak["write_mbps", "medium"]),
            ("write_mbps", "small"): peak["write_mbps", "small"]}
    tops["write_mbps", "medium"] = tops["write_mbps", "large"]
    for w in WORKLOADS:
        tops["verify_mbps", w] = max(peak["verify_mbps", v] for v in WORKLOADS)
    for j, w in enumerate(WORKLOADS):
        sub = ok[ok["workload"] == w]
        for i, metric in enumerate(PHASES):
            ax = axes[i, j]
            _grid(ax)
            for d in order:
                p = sub[sub["drive_id"] == d].sort_values("cycle")
                if p.empty:
                    continue
                g, dock = p["group"].iloc[0], p["path"].iloc[0] == "dock"
                ax.plot(p["cycle"], p[metric], color=style.GROUP_COLORS[g], lw=1.0, zorder=2,
                        solid_joinstyle="round", solid_capstyle="round")
                ax.plot(p["cycle"], p[metric], ls="none", zorder=3, **_marker(g, dock, 3.4))
            top = tops[metric, w]
            _nice_axis(ax, top * 1.08 if np.isfinite(top) and top > 0 else 1.0)
            ax.xaxis.set_major_locator(MaxNLocator(5, integer=True))
            ax.set_xlim(left=0)
            if i == 0:
                ax.set_title(f"{w.capitalize()} files, {SIZES[w]}", fontsize=7, pad=4, loc="left")
            if j == 0:
                ax.set_ylabel(f"{PHASES[metric]} throughput (MB/s)")
            if i == 1:
                ax.set_xlabel("Cycle $k$")
            style.panel_label(ax, "abcdef"[3 * i + j], x=-0.2, y=1.02)
    fig.legend(handles=_group_handles(), loc="upper center", ncol=4, fontsize=6.3,
               bbox_to_anchor=(0.5, 1.0), handlelength=1.8, columnspacing=1.4)
    fig.tight_layout(rect=(0, 0, 1, 0.945), h_pad=1.0, w_pad=1.4)
    return style.save(fig, out / "fig_throughput", width=style.PAGE_W)


# -- Fig. groups ---------------------------------------------------------------------------
def fig_groups(drives: pd.DataFrame, out: Path) -> list[Path]:
    """Median throughput of every drive by workload, one point per drive."""
    fig, axes = plt.subplots(1, 2, figsize=(style.COLUMN_W, 2.05))
    offsets = {"S8": -0.34, "A8": 0.0, "A16": 0.34}
    step = 0.13  # between the drives of one group: markers side by side, not stacked
    for ax, (prefix, label) in zip(axes, (("write", "Write"), ("read", "Read"))):
        _grid(ax)
        shown = []
        for x, w in enumerate(WORKLOADS):
            for g in GROUPS:
                sub = drives[drives["group"] == g]
                vals = sub[f"{prefix}_{w}"].to_numpy(float)
                dock = (sub["path"] == "dock").to_numpy()
                if not np.isfinite(vals).any():
                    continue
                cx = x + offsets[g]
                spread = (np.arange(len(vals)) - (len(vals) - 1) / 2) * step
                for v, dk, dx in zip(vals, dock, spread):
                    if np.isfinite(v):
                        shown.append(v)
                        ax.plot(cx + dx, v, ls="none", zorder=3, **_marker(g, bool(dk), 3.3))
        ax.set_xticks(range(len(WORKLOADS)))
        ax.set_xticklabels([w.capitalize() for w in WORKLOADS])
        ax.tick_params(axis="x", length=0)
        if shown and max(shown) / min(shown) < 4:  # a narrow range reads best on a linear axis
            _nice_axis(ax, max(shown) * 1.05, n=3)
        else:  # every drive inside the frame, with a margin on the log scale
            _log_axis(ax, "y")
            if shown:
                ax.set_ylim(min(shown) / 1.35, max(shown) * 1.35)
        ax.set_ylabel(f"{label} throughput (MB/s)")
        ax.set_xlim(-0.6, len(WORKLOADS) - 0.4)
    fig.legend(handles=_group_handles(line=False), loc="upper center", ncol=2,
               bbox_to_anchor=(0.5, 1.0), fontsize=5.8, columnspacing=1.2,
               handletextpad=0.3, labelspacing=0.25)
    fig.tight_layout(rect=(0, 0, 1, 0.84), w_pad=1.6)
    style.panel_label(axes[0], "a", x=-0.38)
    style.panel_label(axes[1], "b", x=-0.38)
    return style.save(fig, out / "fig_groups", width=style.COLUMN_W)


# -- Fig. phases ---------------------------------------------------------------------------
PHASE_SECONDS = {"write": "write_s", "verify": "verify_s", "delete": "delete_s"}


def phase_table(cycles: pd.DataFrame) -> pd.DataFrame:
    """Hours of each timed phase, per drive and workload, from a representative cycle.

    The representative cycle is the passing cycle of median duration (the lower one of
    an even count), so its phases add up to the duration it is labelled with. A drive
    that passed no cycle of a workload is represented by a failed cycle of it that got
    as far as deleting; its delete time is then the time until the failure, and
    ``outcome`` names the failure.
    """
    rows = []
    for (d, w), sub in cycles.groupby(["drive_id", "workload"], sort=True):
        ok = sub[sub["outcome"] == "pass"].sort_values("duration_s")
        if len(ok):
            rep = ok.iloc[(len(ok) - 1) // 2]
            delete_s = float(rep["delete_s"])
        else:
            deleting = sub[pd.to_numeric(sub["verify_s"], errors="coerce").fillna(0) > 0]
            if deleting.empty:
                if (sub["outcome"] != "pass").any():  # failed, but its phases were not logged
                    rep = sub[sub["outcome"] != "pass"].iloc[0]
                    rows.append({"drive_id": d, "group": rep["group"], "workload": w, "n": 0,
                                 "cycle": int(rep["cycle"]), "outcome": str(rep["outcome"]),
                                 "logged": False})
                continue
            rep = deleting.iloc[0]
            delete_s = float(rep["duration_s"]) - float(rep["write_s"]) - float(rep["verify_s"])
        row = {"drive_id": d, "group": rep["group"], "workload": w, "n": int(len(ok)),
               "cycle": int(rep["cycle"]), "outcome": str(rep["outcome"]), "logged": True,
               "files": int(rep["planned_files"]), "total_h": float(rep["duration_s"]) / 3600}
        for p, col in PHASE_SECONDS.items():
            row[f"{p}_h"] = (delete_s if p == "delete" else float(rep[col])) / 3600
        row["write_files_per_s"] = float(rep["planned_files"]) / max(float(rep["write_s"]), 1e-3)
        row["delete_files_per_s"] = float(rep["planned_files"]) / max(delete_s, 1e-3)
        rows.append(row)
    return pd.DataFrame(rows)


def _drive_rows(campaign: Campaign) -> tuple[dict[str, float], float]:
    """y position of every drive, lots separated by a small gap (as in the timeline)."""
    rows, y = {}, 0.0
    order = drive_order(campaign)
    for g in GROUPS:
        for d in order:
            if campaign.inventory[d].group.code == g:
                rows[d] = y
                y += 1.0
        y += 0.45
    return rows, y - 0.45


def fig_phases(campaign: Campaign, phases: pd.DataFrame, out: Path) -> list[Path]:
    """Where the hours of a cycle go: write, read-back and delete time of every drive."""
    rows, bottom = _drive_rows(campaign)
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 2.35), sharey=True,
                             gridspec_kw={"width_ratios": [1.0, 1.0, 2.3]})
    peaks = {}
    for ax, w in zip(axes, WORKLOADS):
        _grid(ax, "x")
        sub = phases[phases["workload"] == w] if len(phases) else phases
        peak = 0.0
        for d, yy in rows.items():
            r = sub[sub["drive_id"] == d] if len(sub) else sub
            if r.empty:
                ax.text(0.02, yy, "no cycle completed", transform=ax.get_yaxis_transform(),
                        va="center", fontsize=5.6, color=style.MIST)
                continue
            r = r.iloc[0]
            if not r["logged"]:
                ax.text(0.02, yy, f"cycle {r['cycle']} failed; phase times not logged",
                        transform=ax.get_yaxis_transform(), va="center", fontsize=5.6,
                        color=style.FAIL)
                continue
            left = 0.0
            for p in PHASE_SECONDS:
                ax.barh(yy, r[f"{p}_h"], left=left, height=0.64, color=style.PHASE_COLORS[p],
                        edgecolor="white", linewidth=0.6, zorder=2)
                left += r[f"{p}_h"]
            peak = max(peak, left)
            r_total = r["total_h"]
            label = f"  {r_total:.1f} h" if r_total >= 1 else f"  {60 * r_total:.0f} min"
            failed = r["outcome"] != "pass"  # no passing cycle: deleting until the failure
            ax.text(left, yy, label + (", failed" if failed else ""), va="center", ha="left",
                    fontsize=5.8, color=style.FAIL if failed else style.GRAY)
        peaks[w] = peak
        ax.xaxis.set_major_locator(MaxNLocator(4))
        ax.set_title(f"{w.capitalize()} files, {SIZES[w]}", fontsize=7, pad=4, loc="left")
        ax.set_xlabel("Hours per cycle")
        ax.spines["left"].set_visible(False)
        ax.tick_params(axis="y", length=0)
    # large and medium cycles share one scale, so their bars compare directly
    both = max(peaks["large"], peaks["medium"])
    for ax, w in zip(axes, WORKLOADS):
        top = both if w != "small" else peaks[w]
        ax.set_xlim(0, top * 1.38 if top > 0 else 1.0)  # room for "16.4 h, failed"
    ax0 = axes[0]
    ax0.set_yticks(list(rows.values()))
    ax0.set_yticklabels([f"{d}  {campaign.assignments[d].port}" for d in rows], fontsize=6.5)
    ax0.tick_params(axis="y", length=0, pad=10)
    for d, yy in rows.items():
        g = campaign.inventory[d].group.code
        ax0.plot([-0.025], [yy], transform=ax0.get_yaxis_transform(), marker="s", ms=4.0,
                 color=style.GROUP_COLORS[g], clip_on=False, zorder=5)
    ax0.set_ylim(bottom + 0.55, -0.6)
    handles = [Patch(color=style.PHASE_COLORS[p], label=style.PHASE_LABELS[p])
               for p in PHASE_SECONDS]
    fig.legend(handles=handles, loc="upper center", ncol=3, fontsize=6.3,
               bbox_to_anchor=(0.5, 1.02), handlelength=1.4, columnspacing=1.6)
    fig.tight_layout(rect=(0, 0, 1, 0.9), w_pad=2.2)
    fig_w = fig.get_size_inches()[0]
    for ax, letter in zip(axes, "abc"):  # letters a fixed 0.16 in left of b and c
        width_in = ax.get_position().width * fig_w
        style.panel_label(ax, letter, x=-0.5 if ax is ax0 else -0.16 / width_in, y=1.02)
    return style.save(fig, out / "fig_phases", width=style.PAGE_W)


# -- Fig. degradation ----------------------------------------------------------------------
def fig_degradation(campaign: Campaign, cycles: pd.DataFrame, slopes: pd.DataFrame,
                    baseline_cycles: int, out: Path) -> list[Path]:
    """Write throughput relative to each drive's own baseline; fitted trend where it falls."""
    ok = cycles[cycles["outcome"] == "pass"]
    paths = normalised_paths(ok, metric="write_mbps", baseline_cycles=baseline_cycles)
    fits = slopes[slopes["metric"] == "write_mbps"] if len(slopes) else slopes
    order = drive_order(campaign)
    fig, axes = plt.subplots(1, 3, figsize=(style.PAGE_W, 2.15), sharey=True)
    for ax, w in zip(axes, WORKLOADS):
        _grid(ax)
        ax.axhline(1.0, color=style.MIST, lw=0.7, zorder=1)
        ax.axhline(0.5, color=style.FAIL, lw=0.8, ls=(0, (3, 2)), zorder=1)
        sub = paths[paths["workload"] == w]
        falling = 0
        for d in order:
            p = sub[sub["drive_id"] == d].sort_values("cycle")
            if p.empty:
                continue
            g, dock = p["group"].iloc[0], p["path"].iloc[0] == "dock"
            ax.plot(p["cycle"], p["relative"], color=style.GROUP_COLORS[g], lw=0.9, zorder=2)
            ax.plot(p["cycle"], p["relative"], ls="none", zorder=3, **_marker(g, dock, 3.2))
            r = fits[(fits["drive_id"] == d) & (fits["workload"] == w)] if len(fits) else fits
            if len(r) and _true(r["significant_decline"].iloc[0]):
                falling += 1
                k = np.linspace(p["cycle"].min(), p["cycle"].max(), 40)
                ax.plot(k, np.exp(r["intercept"].iloc[0] + r["slope"].iloc[0] * k),
                        color=style.GROUP_COLORS[g], lw=1.6, alpha=0.55, zorder=2)
        # a trend needs three cycles of the workload, so only those drives are counted
        fitted = fits[(fits["workload"] == w) & fits["slope"].notna()] \
            if len(fits) and "slope" in fits else fits.iloc[0:0]
        n = fitted["drive_id"].nunique()
        note = (f"significant decline: {falling} of {n} drives with 3+ cycles" if n
                else "no drive has 3 cycles of this size")
        ax.text(0.03, 0.05, note, transform=ax.transAxes, fontsize=6, color=style.GRAY)
        ax.set_title(f"{w.capitalize()} files", fontsize=7, pad=4, loc="left")
        ax.set_xlabel("Cycle $k$")
        ax.set_xlim(left=0)
        ax.xaxis.set_major_locator(MaxNLocator(5, integer=True))
        ax.set_ylim(0.0, 1.5)
        ax.set_yticks([0.0, 0.5, 1.0, 1.5])
    axes[0].set_ylabel("Write throughput / own baseline")
    axes[0].text(0.03, 0.535, "D1 slowdown threshold (50 %)", transform=axes[0].get_yaxis_transform(),
                 fontsize=5.8, color=style.FAIL, va="bottom")
    for ax, letter in zip(axes, "abc"):
        style.panel_label(ax, letter, x=-0.12, y=1.02)
    fig.legend(handles=_group_handles(), loc="upper center", ncol=4, fontsize=6.3,
               bbox_to_anchor=(0.5, 1.0), handlelength=1.8, columnspacing=1.4)
    fig.tight_layout(rect=(0, 0, 1, 0.9), w_pad=1.0)
    return style.save(fig, out / "fig_degradation", width=style.PAGE_W)


# -- Fig. bounds ---------------------------------------------------------------------------
def fig_bounds(life: pd.DataFrame, planned_cycles: dict[str, float], out: Path) -> list[Path]:
    """Weibayes lower bound on theta (90 %) against the assumed Weibull shape."""
    started = life[life["cycles"] > 0]
    shapes = np.linspace(1.0, 4.0, 121)
    fig, ax = plt.subplots(figsize=(style.COLUMN_W, 2.1))
    _grid(ax)
    sets = [("All drives", started, style.INK, 1.4)] + \
        [(g, started[started["group"] == g], style.GROUP_COLORS[g], 1.1) for g in GROUPS]
    peak = 0.0
    for name, sub, color, lw in sets:
        if sub.empty:
            continue
        t = sub["t_hard"].to_numpy(float)
        d = sub["hard_failed"].to_numpy(bool)
        bound = [weibayes_lower_bound(t, d, s, LEVEL) for s in shapes]
        peak = max(peak, max(bound))
        ax.plot(shapes, bound, color=color, lw=lw, solid_capstyle="round",
                label=style.GROUP_LABELS.get(name, name), zorder=3)
    if planned_cycles and not started.empty:
        t_plan = np.array([planned_cycles[g] for g in started["group"]], float)
        d_none = np.zeros(len(t_plan), bool)
        bound = [weibayes_lower_bound(t_plan, d_none, s, LEVEL) for s in shapes]
        peak = max(peak, max(bound))
        ax.plot(shapes, bound, color=style.MIST, lw=1.0, ls=(0, (3, 2)),
                label="planned, all drives", zorder=2)
    t = started["t_hard"].to_numpy(float)
    d = started["hard_failed"].to_numpy(bool)
    if len(t):
        at2 = weibayes_lower_bound(t, d, 2.0, LEVEL)
        ax.plot(2.0, at2, marker="o", ms=4.2, mfc=style.INK, mec="white", mew=0.8, zorder=4)
        # above and to the left of the point, in the free band between the pooled and the
        # SanDisk curves, so the text never crosses a line
        ax.annotate(f"$\\theta_L$ = {at2:.0f} cycles at $\\gamma$ = 2", xy=(2.0, at2),
                    xytext=(-5, 2.5), textcoords="offset points", ha="right", va="bottom",
                    fontsize=6, color=style.INK)
    ax.set_xlabel(r"Assumed Weibull shape $\gamma$")
    ax.set_ylabel(r"Lower bound $\theta_L$, 90 % (cycles)")
    _nice_axis(ax, peak * 1.02)
    ax.set_xlim(1.0, 4.0)
    # inside the empty upper right, where every curve has fallen or stays low
    ax.legend(loc="upper right", fontsize=5.8, handlelength=1.6, borderaxespad=0.2)
    fig.tight_layout()
    return style.save(fig, out / "fig_bounds", width=style.COLUMN_W)


# -- Fig. context --------------------------------------------------------------------------
#: Earlier wear-out tests of low-cost drives (test plan, Section II): GANGRENE's five
#: 2 GB drives failed after 100 to 315 full-drive writes; Lui's three 8 GB drives after
#: 632, 959 and 9,751 drive-filling cycles.
GANGRENE_FDW = (100.0, 315.0)
LUI_CYCLES = (632.0, 959.0, 9751.0)


def fig_context(campaign: Campaign, drives: pd.DataFrame, out: Path, size: tuple | None = None,
                name: str = "fig_context", legend_cols: int = 3) -> list[Path]:
    """Data written by each drive, in full-drive writes, against earlier failures.

    ``size`` (inches) draws a smaller copy with the same type, for a slide.
    """
    fig_w, fig_h = size or (style.COLUMN_W, 2.35)
    order = drive_order(campaign)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    _grid(ax, "x")
    n = len(order)
    ys = {d: n - 1 - i for i, d in enumerate(order)}
    ax.axvspan(100, 1000, color=BAND, lw=0, zorder=0)
    ax.text(316, n + 0.55, "earlier failures:\n7 of 8 in this band", ha="center", va="bottom",
            fontsize=5.8, color=style.GRAY, linespacing=1.1)
    for d in order:
        r = drives[drives["drive_id"] == d].iloc[0]
        ax.plot(r["full_drive_writes"], ys[d], ls="none", zorder=3,
                **_marker(r["group"], r["path"] == "dock", 4.4))
        v = r["full_drive_writes"]
        label = f"{v:.1f}" if v < 9.95 else f"{v:.0f}"  # two digits, as in the text
        ax.text(r["full_drive_writes"] * 1.18, ys[d], label, va="center", fontsize=5.8,
                color=style.INK)
        ended = "failed" if _true(r["hard_failed"]) else (
            "stopped by a failure" if r["status"] == "attention" else
            "intermittent failure" if r["cycle_failures"] > 0 else "")
        if ended:  # a drive that this test failed, next to the failures of earlier tests
            ax.annotate(ended, (r["full_drive_writes"] * 1.18, ys[d]),
                        xytext=(3.3 * len(label) + 3, 0), textcoords="offset points",
                        va="center", fontsize=5.8, color=style.FAIL)
    lit_y = n + 0.15
    ax.plot(GANGRENE_FDW, [lit_y] * 2, color=style.FAIL, lw=2.2, solid_capstyle="butt", zorder=3)
    ax.plot(LUI_CYCLES, [lit_y] * 3, ls="none", marker="x", ms=4, mew=1.0, color=style.FAIL,
            zorder=3)
    ax.set_yticks([*ys.values(), lit_y])
    ax.set_yticklabels([*ys.keys(), "earlier tests"], fontsize=6.2)
    ax.tick_params(axis="y", length=0)
    ax.set_xscale("log")
    ax.xaxis.set_major_locator(LogLocator(base=10))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    fdw = drives["full_drive_writes"].to_numpy(float)
    fdw = fdw[np.isfinite(fdw) & (fdw > 0)]
    low = 10.0 ** np.floor(np.log10(fdw.min() / 1.5)) if len(fdw) else 1.0
    ax.set_xlim(min(low, 5.0), 20000)
    ax.set_ylim(-0.7, n + 1.6)
    ax.set_xlabel("Full-drive writes (data written / usable capacity)")
    handles = _group_handles(line=False)
    handles += [Line2D([], [], color=style.FAIL, lw=2.2, label="GANGRENE failures"),
                Line2D([], [], ls="none", marker="x", ms=4, mew=1.0, color=style.FAIL,
                       label="Lui failures")]
    fig.legend(handles=handles, loc="lower center", ncol=legend_cols,
               bbox_to_anchor=(0.5, -0.02), fontsize=5.6, columnspacing=0.9, handlelength=1.4)
    rows = math.ceil(len(handles) / legend_cols)  # legend rows below the plot
    band = 0.085 if size is None else (0.05 + 0.09 * rows) / fig_h
    fig.tight_layout(rect=(0, band, 1, 1))
    return style.save(fig, out / name, width=fig_w)


def fig_diagrams(marks: dict, out: Path) -> list[Path]:
    """The plan's rig diagram with the software as run, and the cycle flowchart."""
    pilot, full = _naive(marks.get("pilot_start")), _naive(marks.get("full_start"))
    when = (f"pilot {pilot:%H:%M}, other six {full:%H:%M}" if pilot is not None
            and full is not None else "pilot first, then the other six")
    written = style.save(diagrams.rig_figure(host_lines=(
        "two flashrel supervisors", "8 drive workers, one process each", when)),
        out / "fig_rig", width=style.PAGE_W)
    written += style.save(diagrams.cycle_flowchart(), out / "fig_cycle", width=style.PAGE_W)
    return written


def all_figures(campaign: Campaign, cycles: pd.DataFrame, life: pd.DataFrame,
                drives: pd.DataFrame, slopes: pd.DataFrame, marks: dict,
                planned_cycles: dict[str, float], out: Path,
                events: pd.DataFrame | None = None) -> list[Path]:
    style.apply()
    written: list[Path] = fig_diagrams(marks, out)
    written += fig_timeline(campaign, cycles, life, marks, out,
                            cut_short_cycles(cycles, events, marks))
    written += fig_throughput(campaign, cycles, out)
    written += fig_groups(drives, out)
    written += fig_phases(campaign, phase_table(cycles), out)
    written += fig_degradation(campaign, cycles, slopes, campaign.failure.baseline_cycles, out)
    written += fig_bounds(life, planned_cycles, out)
    written += fig_context(campaign, drives, out)
    return written
