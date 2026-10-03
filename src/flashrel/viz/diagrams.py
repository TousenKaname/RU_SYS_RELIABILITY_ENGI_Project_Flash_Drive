"""Schematic figures: the test rig, the test cycle with failure handling, the schedule.

The drawings are built from a few primitives in millimetre coordinates, so
they come out as crisp vectors at their printed size and share the typography
and colours of the data figures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Polygon, Rectangle

from flashrel.viz.style import (
    FAIL,
    GRAY,
    GROUP_COLORS,
    GROUP_LABELS,
    HAIRLINE,
    INK,
    MIST,
    MM,
    PAGE_W,
    PAPER,
    PASS,
    text_on,
)

DATA = "#3B6E8F"  # data-flow lines


# -- primitives -------------------------------------------------------------------------------
def _canvas(width_mm: float, height_mm: float):
    fig = plt.figure(figsize=(width_mm * MM, height_mm * MM))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, width_mm)
    ax.set_ylim(0, height_mm)
    ax.set_aspect("equal")
    ax.axis("off")
    return fig, ax


def _box(ax, x, y, w, h, *, fc="white", ec=INK, lw=0.6, r=1.2, ls="-", z=2):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                fc=fc, ec=ec, lw=lw, ls=ls, zorder=z))


def _text(ax, x, y, s, *, size=6.5, weight="normal", color=INK, ha="center", va="center",
          style="normal", z=5, **kw):
    ax.text(x, y, s, fontsize=size, fontweight=weight, color=color, ha=ha, va=va,
            fontstyle=style, zorder=z, linespacing=1.25, **kw)


def _poly(ax, pts, *, color=INK, lw=0.6, ls="-", z=1):
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color=color, lw=lw, ls=ls, zorder=z, solid_capstyle="butt")


def _arrow(ax, p0, p1, *, color=INK, lw=0.6, ls="-", head=4.5, z=3, rad=0.0):
    ax.add_patch(FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=head, color=color,
                                 lw=lw, ls=ls, zorder=z, shrinkA=0, shrinkB=0,
                                 connectionstyle=f"arc3,rad={rad}"))


def _section(ax, x, y, s, ha="left"):
    _text(ax, x, y, s.upper(), size=5.6, color=GRAY, ha=ha, weight="bold")


def _monitor(ax, x, y, s=1.0):
    ax.add_patch(Rectangle((x, y + 1.6 * s), 7 * s, 4.4 * s, fc=PAPER, ec=INK, lw=0.5, zorder=4))
    _poly(ax, [(x + 3.5 * s, y + 1.6 * s), (x + 3.5 * s, y + 0.5 * s)], lw=0.5, z=4)
    _poly(ax, [(x + 2 * s, y + 0.5 * s), (x + 5 * s, y + 0.5 * s)], lw=0.5, z=4)


def _laptop(ax, x, y, s=1.0):
    ax.add_patch(Rectangle((x + 0.8 * s, y + 1.4 * s), 5.4 * s, 3.8 * s, fc=PAPER, ec=INK,
                           lw=0.5, zorder=4))
    ax.add_patch(Polygon([(x, y + 0.4 * s), (x + 7 * s, y + 0.4 * s), (x + 6.2 * s, y + 1.4 * s),
                          (x + 0.8 * s, y + 1.4 * s)], closed=True, fc=PAPER, ec=INK, lw=0.5,
                         zorder=4))


def _usb_stick(ax, x, y, label, color, *, w=17.0, h=4.4, probe=True):
    """A drive plugged into a port at (x, y): connector, body, unit ID, probe dot."""
    ax.add_patch(Rectangle((x, y - 1.25), 2.6, 2.5, fc="white", ec=INK, lw=0.5, zorder=4))
    _box(ax, x + 2.6, y - h / 2, w, h, fc=color, ec=color, lw=0.5, r=1.0, z=4)
    _text(ax, x + 2.6 + w / 2 - (1.2 if probe else 0), y, label, size=5.9, weight="bold",
          color=text_on(color))
    if probe:
        ax.plot(x + 2.6 + w - 2.0, y, marker="o", ms=2.3, mfc=INK, mec="white", mew=0.4,
                zorder=6)


def _port(ax, x, y, label):
    ax.add_patch(Rectangle((x - 2.2, y - 1.5), 2.2, 3.0, fc=PAPER, ec=GRAY, lw=0.45, zorder=3))
    _text(ax, x - 3.2, y + 2.4, label, size=5.2, color=GRAY, ha="right")


# -- figure: test rig -------------------------------------------------------------------------
@dataclass(frozen=True)
class RigDrive:
    drive_id: str
    group: str
    port: str


DEFAULT_RIG = {
    "W-direct": [RigDrive("S8-01", "S8", "W1"), RigDrive("A16-01", "A16", "W2"),
                 RigDrive("A8-01", "A8", "W3")],
    "W-dockA": [RigDrive("A8-02", "A8", "A1"), RigDrive("A8-03", "A8", "A2"),
                RigDrive("A8-04", "A8", "A3")],
    "M-dockB": [RigDrive("S8-02", "S8", "B1"), RigDrive("A16-02", "A16", "B2"),
                RigDrive("A8-05", "A8", "B3")],
}


def rig_figure(rig: dict[str, list[RigDrive]] | None = None):
    """Panel a: physical layout of hosts, hubs, drives and probes. Panel b: data path."""
    rig = rig or DEFAULT_RIG
    fig, ax = _canvas(182, 88)

    # ---------------- panel a: physical rig ----------------
    _text(ax, 1.5, 85.5, "a", size=8, weight="bold", ha="left")
    _section(ax, 6, 85.5, "Host computers")
    _section(ax, 57, 85.5, "Hubs")
    _section(ax, 93, 85.5, "Drives under test")

    rows = {"W-direct": (78.0, 72.0, 66.0), "W-dockA": (56.0, 50.0, 44.0),
            "M-dockB": (30.0, 24.0, 18.0)}
    port_x, stick_x = 90.0, 90.0

    # Host W
    _box(ax, 4, 52, 40, 24, fc="white")
    _monitor(ax, 34, 66.5, 1.0)
    _text(ax, 7, 72.2, "Host W", size=7.4, weight="bold", ha="left")
    _text(ax, 7, 68.4, "Windows 11 desktop", size=6, color=GRAY, ha="left")
    _poly(ax, [(7, 65.6), (41, 65.6)], color=HAIRLINE, lw=0.5)
    _text(ax, 7, 62.2, "flashrel supervisor", size=5.9, ha="left")
    _text(ax, 7, 58.9, "6 drive workers (1 process each)", size=5.9, ha="left")
    _text(ax, 7, 55.6, "3 rear-panel USB ports + dock A", size=5.9, ha="left")

    # Host M
    _box(ax, 4, 12, 40, 24, fc="white")
    _laptop(ax, 34, 27.5, 1.0)
    _text(ax, 7, 32.2, "Host M", size=7.4, weight="bold", ha="left")
    _text(ax, 7, 28.4, "macOS laptop", size=6, color=GRAY, ha="left")
    _poly(ax, [(7, 25.6), (41, 25.6)], color=HAIRLINE, lw=0.5)
    _text(ax, 7, 22.2, "flashrel supervisor", size=5.9, ha="left")
    _text(ax, 7, 18.9, "3 drive workers", size=5.9, ha="left")
    _text(ax, 7, 15.6, "USB-C dock B", size=5.9, ha="left")

    # Arduino temperature logger (between the hosts)
    _box(ax, 4, 39.5, 40, 9.5, fc=PAPER, ec=MIST)
    _text(ax, 7, 46.0, "Arduino + DS18B20 probes", size=5.9, weight="bold", ha="left")
    _text(ax, 7, 42.6, "10 probes · every 10 s · USB serial to W", size=5.5,
          color=GRAY, ha="left")
    _poly(ax, [(24, 49), (24, 52)], lw=0.5)

    # Host W -> rear ports (bus at x = 50)
    _poly(ax, [(44, 70), (50, 70)])
    _poly(ax, [(50, rows["W-direct"][0]), (50, rows["W-direct"][-1])])
    for y in rows["W-direct"]:
        _poly(ax, [(50, y), (port_x - 2.2, y)])
    _text(ax, 66, 81.2, "rear-panel ports", size=5.4, color=GRAY, style="italic")

    # Host W -> dock A, Host M -> dock B
    for (dock, label, sub), host_y, ys in (
            (("Dock A", "USB hub", "3 ports"), 58.0, rows["W-dockA"]),
            (("Dock B", "USB-C hub", "3 ports"), 24.0, rows["M-dockB"])):
        y0, y1 = min(ys) - 4.5, max(ys) + 4.5
        _box(ax, 58, y0, 18, y1 - y0, fc=PAPER, ec=INK)
        _text(ax, 67, (y0 + y1) / 2 + 3.2, dock, size=6.6, weight="bold")
        _text(ax, 67, (y0 + y1) / 2 - 0.4, label, size=5.4, color=GRAY)
        _text(ax, 67, (y0 + y1) / 2 - 3.6, sub, size=5.4, color=GRAY)
        mid = (y0 + y1) / 2
        _poly(ax, [(44, host_y), (51, host_y), (51, mid), (58, mid)] if host_y != mid
              else [(44, mid), (58, mid)])
        for y in ys:
            _poly(ax, [(76, y), (port_x - 2.2, y)])

    # Ports and drives
    for key, ys in rows.items():
        for y, d in zip(ys, rig[key]):
            _port(ax, port_x, y, d.port)
            _usb_stick(ax, stick_x, y, d.drive_id, GROUP_COLORS[d.group])

    # Reference drive for port checks
    _port(ax, port_x, 8.0, "W4")
    _usb_stick(ax, stick_x, 8.0, "REF", MIST, probe=False)
    _text(ax, 112.5, 8.0, "spare port W4 and\nreference drive (F5)", size=5.3, color=GRAY,
          ha="left")

    # Legend
    lx, ly = 4.0, 4.6
    for i, g in enumerate(("S8", "A8", "A16")):
        x = lx + i * 26.0
        _box(ax, x, ly - 1.6, 5.0, 3.2, fc=GROUP_COLORS[g], ec=GROUP_COLORS[g], r=0.8)
        _text(ax, x + 6.4, ly, GROUP_LABELS[g], size=5.6, ha="left")
    ax.plot(lx + 78.6, ly, marker="o", ms=2.3, mfc=INK, mec="white", mew=0.4)
    _text(ax, lx + 80.4, ly, "probe", size=5.6, ha="left")

    # ---------------- panel b: data path ----------------
    bx = 132.0
    _text(ax, bx - 3.5, 85.5, "b", size=8, weight="bold", ha="left")
    _section(ax, bx, 85.5, "Data path")
    steps = [
        ("Drive workers", "one cycle at a time per drive"),
        ("Local logs", "cycles.csv · events.jsonl · state.json"),
        ("Shared folder", "both hosts + temperature log"),
        ("flashrel analyze / export", "life table, fits, figures"),
        ("Excel workbook", "by brand, capacity, file size"),
    ]
    h, gap, top = 10.8, 4.6, 80.0
    for i, (title, sub) in enumerate(steps):
        y = top - i * (h + gap)
        last = i == len(steps) - 1
        _box(ax, bx, y - h, 46, h, fc=PAPER if not last else "white",
             ec=DATA if last else INK, lw=0.8 if last else 0.6)
        _text(ax, bx + 23, y - h / 2 + 2.0, title, size=6.3, weight="bold")
        _text(ax, bx + 23, y - h / 2 - 2.0, sub, size=5.4, color=GRAY)
        if not last:
            _arrow(ax, (bx + 23, y - h), (bx + 23, y - h - gap), color=DATA, lw=0.7)
    return fig


# -- figure: test cycle and failure handling -------------------------------------------------
def cycle_flowchart():
    fig, ax = _canvas(182, 76)
    dash = (0, (2.5, 1.5))
    _section(ax, 4, 73.5, "One test cycle, repeated until the drive fails or the test stops")
    main = [
        ("Locate drive", "by identity file,\nnot drive letter"),
        ("Plan cycle k", "workload = rotation(k)\nfill 90 % of free space"),
        ("Write", "host → drive\nflush every file"),
        ("Read back", "drive → host, bypass\ncache, compare bytes"),
        ("Delete", "remove files, check\nspace is returned"),
        ("Log and assess", "cycle record, D1 slowdown,\nD3 transient errors"),
    ]
    w, h, gap, x0, y0 = 25.3, 15.0, 4.04, 4.0, 49.0
    centers = []
    for i, (title, sub) in enumerate(main):
        x = x0 + i * (w + gap)
        _box(ax, x, y0, w, h, fc=PAPER if i in (2, 3, 4) else "white")
        _text(ax, x + w / 2, y0 + h - 4.0, title, size=6.6, weight="bold")
        _text(ax, x + w / 2, y0 + 4.8, sub, size=5.4, color=GRAY)
        centers.append(x + w / 2)
        if i:
            _arrow(ax, (x - gap, y0 + h / 2), (x, y0 + h / 2))
    loop_y = y0 + h + 4.5
    _poly(ax, [(centers[-1], y0 + h), (centers[-1], loop_y), (centers[0], loop_y)])
    _arrow(ax, (centers[0], loop_y), (centers[0], y0 + h))
    _text(ax, (centers[0] + centers[-1]) / 2, loop_y + 2.1, "pass: next cycle, k + 1",
          size=5.8, color=GRAY)

    # failure path: from write, read back and delete down to the retry box
    bus_y, y1, h1 = 43.0, 17.0, 20.0
    retry_cx = 15.0
    for i in (2, 3, 4):
        _poly(ax, [(centers[i], y0), (centers[i], bus_y)], color=FAIL, ls=dash)
    _poly(ax, [(centers[4], bus_y), (retry_cx, bus_y)], color=FAIL, ls=dash)
    _arrow(ax, (retry_cx, bus_y), (retry_cx, y1 + h1), color=FAIL, ls=dash)
    _text(ax, 47.0, bus_y + 1.9, "I/O error, data mismatch or disconnect", size=5.5,
          color=FAIL)

    def step(x, w_, title, sub, ec, fc="white"):
        _box(ax, x, y1, w_, h1, fc=fc, ec=ec)
        _text(ax, x + w_ / 2, y1 + h1 - 4.2, title, size=6.6, weight="bold")
        _text(ax, x + w_ / 2, y1 + 7.4, sub, size=5.4, color=GRAY)

    step(4, 22, "Retry × 2", "cleared → D3\ntransient error;\ncycle continues", FAIL)
    step(35, 23, "Cycle failure", "log F1–F4 or F6\nwith chunk-level\ndiagnosis", FAIL)
    step(64, 36, "Automatic recovery check", "1 wait ≤ 120 s to re-enumerate\n"
         "2 test for a read-only lock\n3 write, verify, delete 64 MiB", INK, PAPER)
    _arrow(ax, (26, y1 + h1 / 2), (35, y1 + h1 / 2), color=FAIL)
    _text(ax, 30.5, y1 + h1 / 2 + 1.9, "persists", size=5.0, color=FAIL)
    _arrow(ax, (58, y1 + h1 / 2), (64, y1 + h1 / 2), color=FAIL)

    def pill(x, y, w_, title, sub, ec):
        _box(ax, x, y, w_, 12.0, fc="white", ec=ec, lw=0.8, r=2.0)
        _text(ax, x + w_ / 2, y + 8.4, title, size=6.1, weight="bold", color=ec)
        _text(ax, x + w_ / 2, y + 3.6, sub, size=5.2, color=GRAY)

    pill(109, 28, 32, "Soft failure (D2)", "drive keeps cycling;\nintermittent event logged", PASS)
    pill(109, 11, 32, "Operator re-check", "re-plug into the spare port;\nreformat if needed", INK)
    pill(149, 28, 30, "Port fault (F5)", "reference drive fails the\nold port: not counted", GRAY)
    pill(149, 11, 30, "Hard failure", "fails again:\nlife T = k, retire", FAIL)
    _arrow(ax, (100, 33.0), (109, 34.0), color=PASS)
    _text(ax, 104.5, 35.6, "pass", size=5.2, color=PASS)
    _arrow(ax, (100, 21.0), (109, 17.0), color=INK)
    _text(ax, 104.5, 16.2, "fail", size=5.2, color=INK)
    _arrow(ax, (125, 23.0), (125, 28.0), color=PASS)
    _text(ax, 126.4, 25.5, "passes", size=5.0, color=PASS, ha="left")
    _arrow(ax, (141, 34.0), (149, 34.0), color=GRAY)
    _arrow(ax, (141, 17.0), (149, 17.0), color=FAIL)

    _text(ax, 4, 6.4, "Also a hard failure: 3 cycle failures within 10 cycles (intermittent "
                      "limit), a persistent read-only lock, or a change in reported capacity.",
          size=5.6, color=GRAY, ha="left")
    _text(ax, 4, 2.2, "F1 corruption · F2 I/O error · F3 disconnect or hang · "
                      "F4 read-only · F5 port or host fault · F6 file-system fault",
          size=5.6, color=INK, ha="left")
    return fig


# -- figure: schedule -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Task:
    name: str
    start: date
    end: date
    owner: str
    stream: str  # "prep", "test", "analysis"


@dataclass(frozen=True)
class Milestone:
    name: str
    day: date


STREAM_COLORS = {"prep": "#A9C0D3", "test": "#2B5C8A", "monitor": "#7FA3C4",
                 "analysis": "#D9973F"}
STREAM_LABELS = {"prep": "preparation", "test": "testing", "monitor": "monitoring",
                 "analysis": "analysis and reporting"}


def schedule_figure(tasks: Sequence[Task], milestones: Sequence[Milestone], start: date,
                    end: date, today: date | None = None):
    n = len(tasks)
    fig = plt.figure(figsize=(PAGE_W, 0.17 * n + 0.75))
    ax = fig.add_axes((0.27, 0.14, 0.70, 0.80))
    for i, task in enumerate(tasks):
        y = n - 1 - i
        ax.barh(y, (task.end - task.start).days + 1, left=(task.start - start).days, height=0.56,
                color=STREAM_COLORS[task.stream], edgecolor="none", zorder=2)
        ax.text((task.end - start).days + 1.6, y, task.owner, va="center", fontsize=5.6,
                color=GRAY)
    for m in milestones:
        x = (m.day - start).days + 0.5
        ax.axvline(x, color=HAIRLINE, lw=0.6, zorder=1)
        ax.plot(x, n - 0.25, marker="D", ms=3.6, color=FAIL, zorder=3, clip_on=False)
        ax.text(x, n + 0.35, m.name, rotation=0, ha="center", va="bottom", fontsize=5.6,
                color=FAIL)
    if today is not None:
        ax.axvline((today - start).days, color=INK, lw=0.6, ls=(0, (1, 1.5)))
    ax.set_yticks(range(n))
    ax.set_yticklabels([t.name for t in reversed(tasks)], fontsize=6.3)
    ax.tick_params(axis="y", length=0)
    weeks = []
    d = start
    while d <= end:
        weeks.append(d)
        d += timedelta(days=7)
    ax.set_xticks([(w - start).days for w in weeks])
    ax.set_xticklabels([f"{w.day} {w:%b}" for w in weeks])
    ax.set_xlim(0, (end - start).days + 1)
    ax.set_ylim(-0.6, n + 0.2)
    ax.spines["left"].set_visible(False)
    streams = [s for s in STREAM_COLORS if any(t.stream == s for t in tasks)]
    handles = [Rectangle((0, 0), 1, 1, fc=STREAM_COLORS[s], ec="none") for s in streams]
    ax.legend(handles, [STREAM_LABELS[s] for s in streams], loc="lower left", fontsize=5.8,
              handlelength=1.4, handleheight=0.8, ncol=2, columnspacing=1.2)
    return fig
