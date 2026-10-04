"""Schematic figures: the test rig, the test cycle with failure handling, the schedule.

The drawings are built from a few primitives in millimetre coordinates, so
they come out as crisp vectors at their printed size and share the typography
and colours of the data figures. Colour carries meaning only: group colours
for drives, blue for data flow, vermilion for failures, green for recovery.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle

from flashrel.viz.style import (
    FAIL,
    GRAY,
    GROUP_COLORS,
    GROUP_LABELS,
    HAIRLINE,
    INK,
    MM,
    PAGE_W,
    PAPER,
    PASS,
    text_on,
)

DATA = "#3B6E8F"        # data flow
CABLE = "#8C8C8C"       # USB cables
EDGE = "#A9A9A9"        # outline of neutral boxes
IO_FILL, IO_EDGE = "#EAF0F5", "#A7BACB"          # the three timed I/O phases
FAIL_FILL, FAIL_EDGE = "#FBEDE9", "#E0A595"      # failure handling
PASS_FILL, PASS_EDGE = "#EAF3ED", "#9CC3AC"      # recovery
REF_FILL = "#CFCFCF"                             # reference drive


# -- primitives -------------------------------------------------------------------------------
def _canvas(width_mm: float, height_mm: float):
    fig = plt.figure(figsize=(width_mm * MM, height_mm * MM))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, width_mm)
    ax.set_ylim(0, height_mm)
    ax.set_aspect("equal")
    ax.axis("off")
    return fig, ax


def _box(ax, x, y, w, h, *, fc="white", ec=EDGE, lw=0.5, r=1.2, z=2):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                fc=fc, ec=ec, lw=lw, zorder=z))


def _text(ax, x, y, s, *, size=6.0, weight="normal", color=INK, ha="center", va="center",
          style="normal", z=5, **kw):
    ax.text(x, y, s, fontsize=size, fontweight=weight, color=color, ha=ha, va=va,
            fontstyle=style, zorder=z, linespacing=1.3, **kw)


def _poly(ax, pts, *, color=CABLE, lw=0.6, ls="-", z=1):
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color=color, lw=lw, ls=ls, zorder=z, solid_capstyle="butt",
            solid_joinstyle="miter")


def _arrow(ax, p0, p1, *, color=INK, lw=0.6, ls="-", head=4.2, z=3):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=head, color=color,
                                 lw=lw, ls=ls, zorder=z, shrinkA=0, shrinkB=0))


def _panel(ax, x, y, letter):
    _text(ax, x, y, letter, size=8, weight="bold", ha="left")


def _section(ax, x, y, s, ha="left"):
    _text(ax, x, y, s.upper(), size=5.5, color=GRAY, ha=ha, weight="bold")


def _monitor(ax, x, y):
    ax.add_patch(Rectangle((x, y + 1.7), 7.2, 4.6, fc=PAPER, ec=GRAY, lw=0.45, zorder=4))
    _poly(ax, [(x + 3.6, y + 1.7), (x + 3.6, y + 0.6)], color=GRAY, lw=0.45, z=4)
    _poly(ax, [(x + 2.1, y + 0.6), (x + 5.1, y + 0.6)], color=GRAY, lw=0.45, z=4)


def _host(ax, x, y, w, h, title, subtitle, lines, icon: Callable):
    _box(ax, x, y, w, h)
    icon(ax, x + w - 10.5, y + h - 8.6)
    _text(ax, x + 3, y + h - 4.4, title, size=7.0, weight="bold", ha="left")
    _text(ax, x + 3, y + h - 8.2, subtitle, size=5.7, color=GRAY, ha="left")
    _poly(ax, [(x + 3, y + h - 11.0), (x + w - 3, y + h - 11.0)], color=HAIRLINE, lw=0.5)
    for i, line in enumerate(lines):
        _text(ax, x + 3, y + h - 14.4 - 3.4 * i, line, size=5.7, ha="left")


def _port(ax, x, y, label):
    ax.add_patch(Rectangle((x - 2.0, y - 1.35), 2.0, 2.7, fc="white", ec=GRAY, lw=0.45,
                           zorder=3))
    _text(ax, x - 3.4, y + 2.0, label, size=5.0, color=GRAY, ha="right")


def _drive(ax, x, y, label, color, *, w=18.5, h=4.6, ink=None):
    """A drive plugged into the port at (x, y): metal plug, then the coloured body."""
    ax.add_patch(Rectangle((x, y - 1.05), 2.3, 2.1, fc="#E6E6E6", ec=GRAY, lw=0.4, zorder=4))
    _box(ax, x + 2.3, y - h / 2, w, h, fc=color, ec=color, lw=0.4, r=1.1, z=4)
    _text(ax, x + 2.3 + w / 2, y, label, size=5.8, weight="bold", color=ink or text_on(color))


# -- figure: test rig -------------------------------------------------------------------------
@dataclass(frozen=True)
class RigDrive:
    drive_id: str
    group: str
    port: str


DEFAULT_RIG = {
    "rear": [RigDrive("S8-01", "S8", "W1"), RigDrive("A16-01", "A16", "W2"),
             RigDrive("A8-02", "A8", "W3")],
    "dockA": [RigDrive("A8-01", "A8", "A1"), RigDrive("S8-02", "S8", "A2"),
              RigDrive("A8-03", "A8", "A3")],
    "dockB": [RigDrive("A16-02", "A16", "B1"), RigDrive("A8-04", "A8", "B2"),
              RigDrive("A8-05", "A8", "B3")],
}


def rig_figure(rig: dict[str, list[RigDrive]] | None = None):
    """Panel a: the host, its three connection paths and the drives. Panel b: data path."""
    rig = rig or DEFAULT_RIG
    fig, ax = _canvas(182, 74)
    head = 71.2   # baseline of the panel letters and section headers

    # ---------------- panel a: physical rig ----------------
    _panel(ax, 0.5, head, "a")
    _section(ax, 5, head, "Host")
    _section(ax, 57, head, "Hubs")
    _section(ax, 85, head, "Drives under test")
    rows = {"rear": (66.0, 60.5, 55.0), "dockA": (45.0, 39.5, 34.0),
            "dockB": (23.0, 17.5, 12.0)}
    port_x = 87.0

    _host(ax, 4, 24, 42, 28, "Host W", "Windows desktop",
          ("flashrel supervisor", "9 drive workers, one process each",
           "front port W4 kept as the spare"), _monitor)

    # host -> rear-panel ports (one bus)
    bus_x = 52.0
    _poly(ax, [(46, 48.0), (bus_x, 48.0), (bus_x, rows["rear"][0])])
    for y in rows["rear"]:
        _poly(ax, [(bus_x, y), (port_x - 2.0, y)])
    _text(ax, 66, 68.8, "rear-panel ports", size=5.2, color=GRAY, style="italic")

    # host -> docks -> ports
    for (name, kind), ys, exit_y in ((("Dock A", "USB hub · 3 ports"), rows["dockA"], 39.5),
                                     (("Dock B", "USB hub · 3 ports"), rows["dockB"], 28.0)):
        y0, y1 = min(ys) - 4.4, max(ys) + 4.4
        mid = (y0 + y1) / 2
        _box(ax, 58, y0, 17, y1 - y0, fc=PAPER)
        _text(ax, 66.5, mid + 1.9, name, size=6.4, weight="bold")
        _text(ax, 66.5, mid - 1.9, kind, size=5.0, color=GRAY)
        _poly(ax, [(46, exit_y), (bus_x, exit_y), (bus_x, mid), (58, mid)]
              if exit_y != mid else [(46, mid), (58, mid)])
        for y in ys:
            _poly(ax, [(75, y), (port_x - 2.0, y)])

    for key, ys in rows.items():
        for y, d in zip(ys, rig[key]):
            _port(ax, port_x, y, d.port)
            _drive(ax, port_x, y, d.drive_id, GROUP_COLORS[d.group])

    # spare port and reference drive, used only for port checks
    _port(ax, port_x, 3.8, "W4")
    _drive(ax, port_x, 3.8, "REF", REF_FILL, ink=INK)
    _text(ax, 110.5, 3.8, "spare front port;\nreference drive for port checks (F5)",
          size=5.0, color=GRAY, ha="left")

    # legend
    for i, g in enumerate(("S8", "A8", "A16")):
        x = 4.0 + i * 24.0
        _box(ax, x, 2.3, 4.6, 3.0, fc=GROUP_COLORS[g], ec=GROUP_COLORS[g], r=0.7)
        _text(ax, x + 6.0, 3.8, GROUP_LABELS[g], size=5.6, ha="left")

    # ---------------- panel b: data path ----------------
    _panel(ax, 128.5, head, "b")
    _section(ax, 133, head, "Data path")
    steps = [
        ("Drive workers", "one process per drive on host W"),
        ("Local logs", "cycles.csv · events.jsonl · state.json"),
        ("Daily backup", "log folder copied to cloud storage"),
        ("flashrel analyze", "life table, model fits, figures"),
        ("Excel workbook", "sheets by brand, capacity, and file size"),
    ]
    spine_x, top, step = 135.0, 64.5, 12.6
    for i, (title, sub) in enumerate(steps):
        y = top - i * step
        last = i == len(steps) - 1
        ax.add_patch(Circle((spine_x, y), 1.35, fc=DATA if last else "white", ec=DATA, lw=0.8,
                            zorder=4))
        _text(ax, spine_x + 4.0, y + 1.7, title, size=6.4, weight="bold", ha="left")
        _text(ax, spine_x + 4.0, y - 2.0, sub, size=5.3, color=GRAY, ha="left")
        if not last:
            _arrow(ax, (spine_x, y - 1.35), (spine_x, y - step + 1.35), color=DATA, lw=0.8)
    return fig


# -- figure: test cycle and failure handling -------------------------------------------------
def cycle_flowchart():
    """One cycle (top row) and what happens when a phase fails (bottom rows)."""
    fig, ax = _canvas(182, 79.5)
    dash = (0, (2.4, 1.6))
    _section(ax, 3, 77.0, "One test cycle, repeated until the drive fails or the test stops")

    main = [
        ("Locate drive", "by its identity file,\nnot its drive letter"),
        ("Plan cycle k", "workload from the rotation;\nfill 90 % of free space"),
        ("Write", "host → drive;\nflush every file"),
        ("Read back", "drive → host around the\ncache; compare bytes"),
        ("Delete", "remove the files; check\nthe space comes back"),
        ("Log and assess", "cycle record; slowdown (D1),\ntransient errors (D3)"),
    ]
    w, h, gap, x0, y0 = 26.0, 14.0, 3.6, 3.0, 52.5
    centers = []
    for i, (title, sub) in enumerate(main):
        x = x0 + i * (w + gap)
        timed = i in (2, 3, 4)
        _box(ax, x, y0, w, h, fc=IO_FILL if timed else "white", ec=IO_EDGE if timed else EDGE)
        _text(ax, x + w / 2, y0 + h - 3.6, title, size=6.5, weight="bold")
        _text(ax, x + w / 2, y0 + 4.4, sub, size=5.3, color=GRAY)
        centers.append(x + w / 2)
        if i:
            _arrow(ax, (x - gap, y0 + h / 2), (x, y0 + h / 2))
    loop_y = y0 + h + 4.2
    _poly(ax, [(centers[-1], y0 + h), (centers[-1], loop_y), (centers[0], loop_y)], color=INK)
    _arrow(ax, (centers[0], loop_y), (centers[0], y0 + h))
    _text(ax, (centers[0] + centers[-1]) / 2, loop_y + 1.9, "pass: next cycle, k + 1",
          size=5.6, color=GRAY)

    # failure path from the timed phases down to the retry box
    bus_y, y1, h1 = 44.5, 21.0, 15.0
    retry_cx = 14.5
    for i in (2, 3, 4):
        _poly(ax, [(centers[i], y0), (centers[i], bus_y)], color=FAIL, ls=dash)
    _poly(ax, [(centers[4], bus_y), (retry_cx, bus_y)], color=FAIL, ls=dash)
    _arrow(ax, (retry_cx, bus_y), (retry_cx, y1 + h1), color=FAIL, ls=dash)
    _text(ax, 44.0, bus_y + 1.8, "I/O error, data mismatch, or disconnect", size=5.4, color=FAIL)

    def step(x, w_, title, sub, fc, ec):
        _box(ax, x, y1, w_, h1, fc=fc, ec=ec)
        _text(ax, x + w_ / 2, y1 + h1 - 3.6, title, size=6.5, weight="bold")
        _text(ax, x + w_ / 2, y1 + 5.2, sub, size=5.3, color=GRAY)

    step(3, 23, "Retry, twice", "clears: transient\nerror (D3), cycle\ncontinues", FAIL_FILL,
         FAIL_EDGE)
    step(33, 24, "Cycle failure", "logged as F1–F4 or\nF6, with the kind\nof corruption",
         FAIL_FILL, FAIL_EDGE)
    step(64, 36, "Automatic recovery check", "wait ≤ 120 s for the drive;\n"
         "test for a read-only lock;\nwrite, verify, delete 64 MiB", "white", EDGE)
    _arrow(ax, (26, y1 + h1 / 2), (33, y1 + h1 / 2), color=FAIL)
    _text(ax, 29.5, y1 + h1 / 2 + 1.8, "persists", size=5.0, color=FAIL)
    _arrow(ax, (57, y1 + h1 / 2), (64, y1 + h1 / 2), color=FAIL)

    def outcome(x, y, w_, title, sub, fc, ec, title_color=INK):
        _box(ax, x, y, w_, 11.5, fc=fc, ec=ec, r=1.6)
        _text(ax, x + w_ / 2, y + 7.9, title, size=6.2, weight="bold", color=title_color)
        _text(ax, x + w_ / 2, y + 3.4, sub, size=5.2, color=GRAY)

    top, bottom = 27.5, 8.5   # lower edges of the two rows of outcomes
    outcome(108, top, 32, "Soft failure (D2)", "drive keeps cycling;\nevent is logged",
            PASS_FILL, PASS_EDGE, PASS)
    outcome(108, bottom, 32, "Operator re-check",
            "drive moved to the spare\nport; reformat if needed", "white", EDGE)
    outcome(148, top, 31, "Port fault (F5)", "reference drive also fails\nthe port: not counted",
            PAPER, EDGE, GRAY)
    outcome(148, bottom, 31, "Hard failure", "fails again: life T = k;\ndrive is retired",
            FAIL_FILL, FAIL_EDGE, FAIL)
    _arrow(ax, (100, 32.0), (108, top + 5.75), color=PASS)
    _text(ax, 103.8, 35.4, "pass", size=5.0, color=PASS)
    _arrow(ax, (100, 25.0), (108, bottom + 5.75), color=INK)
    _text(ax, 102.8, 18.6, "fail", size=5.0, color=INK)
    _arrow(ax, (124, bottom + 11.5), (124, top), color=PASS)
    _text(ax, 125.2, (bottom + 11.5 + top) / 2, "passes", size=5.0, color=PASS, ha="left")
    _arrow(ax, (140, top + 5.75), (148, top + 5.75), color=GRAY)
    _arrow(ax, (140, bottom + 5.75), (148, bottom + 5.75), color=FAIL)

    _text(ax, 3, 4.6, "Also a hard failure: three cycle failures within ten cycles, a persistent "
                      "read-only lock, or a change in the reported capacity.",
          size=5.4, color=GRAY, ha="left")
    _text(ax, 3, 1.2, "F1 corruption · F2 I/O error · F3 disconnect or hang · F4 read-only · "
                      "F5 port or host fault · F6 file-system fault",
          size=5.4, color=INK, ha="left")
    return fig


# -- figure: schedule -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Task:
    name: str
    start: datetime
    end: datetime
    owner: str
    stream: str                       # key of STREAM_COLORS
    marks: tuple[datetime, ...] = ()  # point events (drawn as dots) instead of a bar


@dataclass(frozen=True)
class Milestone:
    name: str
    when: datetime
    align: str = "center"             # label alignment, to keep close labels apart


STREAM_COLORS = {"prep": "#A9C0D3", "test": "#2B5C8A", "check": "#2B5C8A",
                 "report": "#D9973F"}
STREAM_LABELS = {"prep": "preparation", "test": "testing (unattended)",
                 "check": "daily status check", "report": "analysis and slides"}
WEEKEND = "#F5F5F3"


def schedule_figure(tasks: Sequence[Task], milestones: Sequence[Milestone], start: datetime,
                    end: datetime):
    """Gantt chart on a one-day grid; weekends shaded; bars at hour resolution."""
    def x(t: datetime) -> float:
        return (t - start).total_seconds() / 86400.0

    n = len(tasks)
    days = int(round(x(end)))
    fig = plt.figure(figsize=(PAGE_W, 0.2 * n + 0.95))
    ax = fig.add_axes((0.24, 0.17, 0.74, 0.62))

    for k in range(days):
        day = start + timedelta(days=k)
        if day.weekday() >= 5:
            ax.axvspan(k, k + 1, color=WEEKEND, lw=0, zorder=0)
        ax.axvline(k, color=HAIRLINE, lw=0.35, zorder=0.5)
    for i, task in enumerate(tasks):
        y = n - 1 - i
        color = STREAM_COLORS[task.stream]
        if task.marks:
            ax.plot([x(t) for t in task.marks], [y] * len(task.marks), ls="none", marker="o",
                    ms=2.6, color=color, zorder=3)
            tail = max(x(t) for t in task.marks)
        else:
            ax.barh(y, x(task.end) - x(task.start), left=x(task.start), height=0.5,
                    color=color, edgecolor="none", zorder=2)
            tail = x(task.end)
        ax.text(tail + 0.18, y, task.owner, va="center", ha="left", fontsize=5.5, color=GRAY)
    for m in milestones:
        mx = x(m.when)
        ax.plot([mx, mx], [-0.6, n - 0.35], color=FAIL, lw=0.5, alpha=0.4, zorder=1)
        ax.plot(mx, n - 0.05, marker="D", ms=3.3, color=FAIL, zorder=4, clip_on=False)
        ax.text(mx, n + 0.45, m.name, ha=m.align, va="bottom", fontsize=5.6, color=FAIL)

    ax.set_yticks(range(n))
    ax.set_yticklabels([t.name for t in reversed(tasks)], fontsize=6.2)
    ax.tick_params(axis="y", length=0, pad=4)
    ax.set_xticks([k + 0.5 for k in range(days)])
    ax.set_xticklabels([f"{'MTWTFSS'[(start + timedelta(days=k)).weekday()]}\n"
                        f"{(start + timedelta(days=k)).day}" for k in range(days)],
                       fontsize=5.6, linespacing=1.4)
    ax.tick_params(axis="x", length=0, pad=2.5)
    for label, k in zip(ax.get_xticklabels(), range(days)):
        if (start + timedelta(days=k)).weekday() >= 5:
            label.set_color(GRAY)
    ax.set_xlim(0, days)
    ax.set_ylim(-0.6, n - 0.35)
    for side in ("left", "right", "top"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_linewidth(0.5)
    ax.text(0.0, -0.36, f"{start:%B %Y}", transform=ax.transAxes, fontsize=5.8, color=GRAY,
            ha="left", va="top")

    streams = [s for s in STREAM_COLORS if any(t.stream == s for t in tasks)]
    handles = []
    for s in streams:
        if s == "check":
            handles.append(plt.Line2D([], [], ls="none", marker="o", ms=2.6,
                                      color=STREAM_COLORS[s]))
        else:
            handles.append(Rectangle((0, 0), 1, 1, fc=STREAM_COLORS[s], ec="none"))
    ax.legend(handles, [STREAM_LABELS[s] for s in streams], loc="upper right",
              bbox_to_anchor=(1.0, -0.2), ncol=len(streams), fontsize=5.6, handlelength=1.3,
              handleheight=0.75, columnspacing=1.4, frameon=False)
    return fig
