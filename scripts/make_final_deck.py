"""Final presentation: the proposal deck, updated to the test as run, plus the results.

    python scripts/make_final_deck.py --proposal ../Proposal/ISE585_Project4_Proposal_v1.pptx \
        --results ../Project/results --out ../Proposal/ISE585_Project4_Final.pptx

The proposal deck stays unchanged. The new deck keeps its design (CONTENT layout,
Georgia titles, Arial text, the brand colors) and its method slides, updates the cover,
the footer and the schedule to the test as run, and adds the results slides built from
``results.json`` and the figures of ``make_results.py``. Charts are native PowerPoint
charts wherever PowerPoint has the form; the timeline is the report figure.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.chart import XL_MARKER_STYLE, XL_TICK_MARK
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

NAVY = RGBColor(0x1B, 0x2A, 0x41)
BODY = RGBColor(0x3B, 0x43, 0x50)
MUTED = RGBColor(0x7C, 0x85, 0x92)
CRIMSON = RGBColor(0xA3, 0x19, 0x2E)
RULE = RGBColor(0xDD, 0xE1, 0xE6)
GRIDLINE = RGBColor(0xE9, 0xEC, 0xEF)
GREEN = RGBColor(0x3F, 0x7F, 0x5F)
GROUPS = ("S8", "A8", "A16")
GROUP_COLOR = {"S8": RGBColor(0x3A, 0x76, 0xB8), "A8": RGBColor(0xD9, 0x97, 0x3F),
               "A16": RGBColor(0x8A, 0x4F, 0x17)}
GROUP_LABEL = {"S8": "SanDisk 8 GB", "A8": "ABLAZE 8 GB", "A16": "ABLAZE 16 GB"}
GROUP_MARKER = {"S8": XL_MARKER_STYLE.CIRCLE, "A8": XL_MARKER_STYLE.SQUARE,
                "A16": XL_MARKER_STYLE.TRIANGLE}
WORKLOADS = ("large", "medium", "small")
#: Cycle phases, light to dark in the order of the report figure (``style.PHASE_COLORS``).
PHASES = ("write", "verify", "delete")
PHASE_COLOR = {"write": RGBColor(0x3F, 0x34, 0x70), "verify": RGBColor(0x7A, 0x69, 0xAD),
               "delete": RGBColor(0xAD, 0xA0, 0xD2)}
PHASE_LABEL = {"write": "Write", "verify": "Read-back", "delete": "Delete"}
#: The validated report palette replaces the proposal's two group colors everywhere.
RECOLOR = {"4A7FB0": "3A76B8", "8C5320": "8A4F17"}
LEFT_X, LEFT_W = 0.75, 3.60
RIGHT_X, RIGHT_W = 4.95, 7.63
FOOTER_OLD, FOOTER_NEW = "Project 4 proposal", "Project 4 final presentation"
NUMBER_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
                8: "Eight"}


def and_list(items: list[str]) -> str:
    """'A', 'A and B', 'A, B, and C'."""
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


# -- low-level helpers --------------------------------------------------------------------
def _runs(paragraph, runs):
    for text, opts in runs:
        r = paragraph.add_run()
        r.text = text
        f = r.font
        f.size = Pt(opts.get("size", 15))
        f.bold = opts.get("bold", False)
        f.italic = opts.get("italic", False)
        f.color.rgb = opts.get("color", BODY)
        if "font" in opts:
            f.name = opts["font"]


def text_box(slide, x, y, w, h, paragraphs, *, name, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP,
             spacing=1.2, space_after=0):
    """Text box in the deck's style: zero insets, 120 % line spacing.

    ``paragraphs`` is a list; each item is a string, or a list of (text, options) runs,
    or a (runs, paragraph options) tuple.
    """
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.name = name
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    for i, item in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(space_after)
        runs = item
        if isinstance(item, str):
            runs = [(item, {})]
        _runs(p, runs)
    return box


def rule(slide, x, y, w, name):
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x), Inches(y),
                                      Inches(x + w), Inches(y))
    line.name = name
    line.line.color.rgb = RULE
    line.line.width = Pt(0.75)
    return line


def stat(slide, x, y, w, value, label, name, value_color=NAVY):
    text_box(slide, x, y, w, 0.6, [[(value, {"size": 34, "font": "Georgia", "color": value_color})]],
             name=f"{name} value", spacing=1.0)
    text_box(slide, x, y + 0.62, w, 0.6, [[(label, {"size": 13, "color": MUTED})]],
             name=f"{name} label", spacing=1.1)


def chart_header(slide, title, subtitle=None, x=RIGHT_X, w=RIGHT_W, y=1.08):
    text_box(slide, x, y, w, 0.35, [[(title, {"size": 13, "bold": True, "color": NAVY})]],
             name="chart title")
    if subtitle:
        text_box(slide, x, y + 0.34, w, 0.3, [[(subtitle, {"size": 12, "color": MUTED})]],
                 name="chart sub")


def note(slide, text, y, x=RIGHT_X, w=RIGHT_W, name="note"):
    return text_box(slide, x, y, w, 0.45, [[(text, {"size": 13, "italic": True, "color": MUTED})]],
                    name=name)


def replace_text(slide, old, new):
    """Replace ``old`` by ``new`` in the runs of a slide (formatting kept); count of hits."""
    hits = 0
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if old in r.text:
                    r.text = r.text.replace(old, new)
                    hits += 1
    return hits


def set_shape_text(slide, name, text):
    """Set the text of the named shape, keeping the formatting of its first run."""
    for shape in slide.shapes:
        if shape.name == name and shape.has_text_frame:
            p = shape.text_frame.paragraphs[0]
            runs = p.runs
            runs[0].text = text
            for r in runs[1:]:
                r.text = ""
            return shape
    raise KeyError(name)


def recolor_xml(element):
    for node in element.iter():
        if node.tag.endswith("}srgbClr"):
            val = node.get("val", "").upper()
            if val in RECOLOR:
                node.set("val", RECOLOR[val])


def style_axis_text(axis, size=11, color=MUTED):
    axis.tick_labels.font.size = Pt(size)
    axis.tick_labels.font.color.rgb = color
    axis.tick_labels.font.name = "Arial"


def bars_top_down(chart):
    """List the categories of a bar chart from the top, with the value axis kept below.

    Reversing the categories moves the value axis to the top unless the value axis crosses
    at the last category. python-pptx exposes ``crosses`` on the value axis only, where it
    moves the other axis, so the value axis's own ``c:crosses`` is set in the XML.
    """
    chart.category_axis.reverse_order = True
    chart._chartSpace.find(".//" + qn("c:valAx")).find(qn("c:crosses")).set("val", "max")


def quiet_chart(chart, legend=True):
    chart.font.name = "Arial"
    chart.font.size = Pt(11)
    chart.font.color.rgb = BODY
    chart.has_legend = legend
    if legend:
        chart.legend.position = XL_LEGEND_POSITION.TOP
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(11)
        chart.legend.font.color.rgb = BODY
    va = chart.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = GRIDLINE
    va.major_gridlines.format.line.width = Pt(0.75)
    va.format.line.fill.background()
    va.major_tick_mark = XL_TICK_MARK.NONE
    style_axis_text(va)
    ca = chart.category_axis
    ca.format.line.color.rgb = RULE
    ca.major_tick_mark = XL_TICK_MARK.NONE
    style_axis_text(ca, 12, BODY)


# -- slides -------------------------------------------------------------------------------
class Deck:
    def __init__(self, proposal: Path, results: Path):
        self.prs = Presentation(str(proposal))
        self.results = results
        self.r = json.loads((results / "results.json").read_text(encoding="utf-8"))
        self.layout = next(l for l in self.prs.slide_layouts if l.name == "CONTENT")
        self.number_template = next(
            sh for sh in self.prs.slides[1].shapes
            if sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER)

    # -- structure ----------------------------------------------------------------------
    def new_slide(self, number: str, title: str, notes: str = ""):
        """A CONTENT slide with its number, title, and slide number like the proposal's.

        The proposal's placeholders carry zero insets on the slide itself, so a new slide
        sets them too; otherwise its title would sit 0.1 in to the right of every other.
        """
        s = self.prs.slides.add_slide(self.layout)
        sp = copy.deepcopy(self.number_template._element)
        s.shapes._spTree.append(sp)
        sp.nvSpPr.cNvPr.id = max(int(e.get("id")) for e in s.shapes._spTree.iter()
                                 if e.tag.endswith("}cNvPr") and e.get("id")) + 1
        for ph in s.placeholders:
            t = ph.placeholder_format.type
            if t in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.BODY):
                tf = ph.text_frame
                tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
                tf.text = title if t == PP_PLACEHOLDER.TITLE else number
        if notes:
            s.notes_slide.notes_text_frame.text = notes
        # Georgia 30 pt in the title box holds about 20 characters per line.
        self.body_y = 2.2 if len(title) <= 20 else 2.55
        return s

    def order(self, slides):
        lst = self.prs.slides._sldIdLst
        ids = {s.slide_id: el for s, el in zip(self.prs.slides, list(lst))}
        wanted = [ids[s.slide_id] for s in slides]  # resolve before the list is emptied
        for el in list(lst):
            lst.remove(el)
        for el in wanted:
            lst.append(el)

    # -- global edits -------------------------------------------------------------------
    def restyle(self):
        for layout in self.prs.slide_layouts:
            for shape in layout.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        for run in p.runs:
                            run.text = run.text.replace(FOOTER_OLD, FOOTER_NEW)
        for slide in self.prs.slides:
            recolor_xml(slide._element)
        master = self.prs.slide_master
        theme = master.part.part_related_by(RT.THEME)
        xml = theme.blob.decode("utf-8")
        xml = re.sub(r'(<a:accent2><a:srgbClr val=")4A7FB0', r"\g<1>3A76B8", xml)
        xml = re.sub(r'(<a:accent3><a:srgbClr val=")8C5320', r"\g<1>8A4F17", xml)
        theme._blob = xml.encode("utf-8")

    def cover(self):
        s = self.prs.slides[0]
        replace_text(s, "Project 4 proposal", "Project 4 final presentation")
        s.notes_slide.notes_text_frame.text = (
            "Final presentation of Project 4: the 24-hour life test of eight USB flash drives "
            "that the proposal planned, run on October 23 and 24, 2026, and its results.")

    def placeholder(self, s, kind):
        return next(sh for sh in s.shapes
                    if sh.is_placeholder and sh.placeholder_format.type == kind)

    def schedule(self, s):
        """Proposal slide 10 becomes the schedule as run."""
        self.placeholder(s, PP_PLACEHOLDER.TITLE).text_frame.paragraphs[0].runs[0].text = \
            "Schedule as Run"
        self.placeholder(s, PP_PLACEHOLDER.BODY).text_frame.paragraphs[0].runs[0].text = "07"
        swaps = [("Fri, Oct 23, 09:00", "Fri, Oct 23, 17:30"), ("10:00", "17:44"),
                 ("12:00–15:00", "Sat, after 20:00"), ("Sat, Oct 24, 12:00", "Sat, Oct 24, 20:00"),
                 ("12:00", "20:00"),
                 ("two drives, S8-01 and A8-01, for two hours",
                  "S8-01 and A8-01; passed all four checks"),
                 ("post-test check of failed drives, export, and analysis",
                  "post-test check of every drive, export, and analysis")]
        for old, new in swaps:
            replace_text(s, old, new)
        for sh in s.shapes:
            if not sh.has_text_frame:
                continue
            for p in sh.text_frame.paragraphs:
                if "".join(r.text for r in p.runs).startswith("The pilot passes when"):
                    p.runs[0].text = ("The test ran eight hours later than planned, with the "
                                      "planned durations: a pilot of two drives, then all eight "
                                      "drives for 24 hours.")
                    for r in p.runs[1:]:
                        r.text = ""
        s.notes_slide.notes_text_frame.text = (
            "Intake started at 17:30 instead of 09:00, so every step moved by eight hours. The "
            "pilot ran on S8-01 and A8-01 from 17:44 and passed; the other six drives started "
            "at 20:00, 30 seconds apart, and every drive stopped at 20:00 on Saturday.")

    def _failed(self) -> list[dict]:
        """Drives that failed hard or were stopped by a failed recovery check."""
        return [d for d in self.r["per_drive"] if d["hard_failed"] or d["status"] == "attention"]

    def analysis_plan(self, s):
        hard = len(self._failed())
        text = ("Result: no drive failed in 24 h, so the second column applies." if hard == 0
                else "Result: one drive failed in 24 h, fewer than two, so the second column "
                     "applies." if hard == 1
                else f"Result: {hard} hard failures, so the first column applies.")
        for sh in s.shapes:
            if sh.name == "analysis note":
                sh.text_frame.paragraphs[0].runs[0].text = text

    # -- new results slides -------------------------------------------------------------
    def glance(self):
        r = self.r
        s = self.new_slide("09", "Results at a Glance", notes=(
            "The headline numbers of the 24-hour test, from the logs. Every file written was "
            "read back around the host's cache and compared byte by byte with its reference."))
        files = r["files_written"]
        files_txt = f"{files / 1e6:.2f} M" if files >= 1e6 else f"{files:,}"
        corrupt = r["bits_corrupt"]
        failed = self._failed()
        lost = sum(d["files_missing"] + d["files_corrupt"] for d in r["per_drive"])
        if not failed and lost == 0 and corrupt == 0:
            lines = ["All eight drives ran for the full 24 hours and every byte they wrote read "
                     "back unchanged."]
        elif len(failed) == 1:
            d = failed[0]
            gone = d["files_missing"] + d["files_corrupt"]
            lines = [f"One drive failed: {d['drive_id']} lost {gone:,} files in cycle "
                     f"{d['cycles']} and did not recover."
                     + (" Every other byte read back unchanged." if corrupt == 0 else "")]
        else:  # grouped by cycle, as on the findings slide
            by_cycle: dict[int, list[str]] = {}
            for d in failed:
                by_cycle.setdefault(int(d["cycles"]), []).append(d["drive_id"])
            lines = [f"{NUMBER_WORDS.get(len(failed), len(failed))} drives failed: "
                     + "; ".join(f"{and_list(sorted(v))} in cycle {c}"
                                 for c, v in sorted(by_cycle.items())) + "."]
        stuck = any(d["hard_code"] in ("F3", "F4") for d in failed)
        lines.append("No drive slowed down, dropped off the bus, or locked read-only."
                     if r["slow_events"] == 0 and not stuck else
                     f"{r['slow_events']} slowdowns were logged.")
        text_box(s, LEFT_X, self.body_y, LEFT_W, 2.6, lines, name="glance text", space_after=8)
        cells = [
            (f"{r['cycles_total']}", "copy-verify-delete cycles"),
            (f"{r['tb_written']:.2f} TB", "written, and read back"),
            (files_txt, "files verified byte by byte"),
            (f"{len(failed)}", "drives failed"),
            (f"{lost:,}", "test files lost or corrupted"),
            (f"{r['transient_errors']}", "transient errors (D3)"),
        ]
        for i, (value, label) in enumerate(cells):
            col, row = i % 3, i // 3
            stat(s, RIGHT_X + col * 2.6, 1.3 + row * 2.0, 2.4, value, label, f"stat {i}",
                 value_color=CRIMSON if (i in (3, 4) and value != "0") else NAVY)
        rule(s, RIGHT_X, 3.05, RIGHT_W, "glance rule")
        note(s, "Counts over all eight drives; per-drive values are in the report and the "
                "workbook.", 5.45)
        return s

    def timeline(self):
        failed = self._failed()
        names = and_list([d["drive_id"] for d in failed])
        red = ("a failed cycle would be red" if not failed else
               f"the red bar is the failed cycle of {names}" if len(failed) == 1 else
               f"the red bars are the failed cycles of {names}")
        s = self.new_slide("10", "Every Cycle", notes=(
            "Each bar is one cycle, from its start to its end, colored by the file size of the "
            "rotation: large, medium, small. Small-file cycles take longest. The pilot drives "
            "started at 17:44; the others at 20:00; all stopped at 20:00 the next day; "
            f"{red}."))
        text_box(s, RIGHT_X, 1.08, RIGHT_W, 0.9, [
            [("Each bar is one cycle, colored by the file size of the rotation. ",
              {"size": 13, "color": BODY}),
             (f"Small-file cycles take the longest; {red}.",
              {"size": 13, "color": MUTED})]], name="timeline text")
        pic = self.results / "fig_timeline.png"
        s.shapes.add_picture(str(pic), Inches(0.75), Inches(2.25), width=Inches(11.83))
        return s

    def throughput(self):
        groups = self.r["per_group_workload"]
        s = self.new_slide("11", "Throughput", notes=(
            "Median throughput of all passing cycles of each group and workload. The two "
            "brands differ in opposite directions: ABLAZE writes faster, SanDisk reads faster. "
            "The 16 GB ABLAZE drives write more slowly than the 8 GB ones, and small files cut "
            "every drive's write throughput to about 1 MB/s or less."))
        text_box(s, LEFT_X, self.body_y, LEFT_W, 3.6, self._throughput_text(groups),
                 name="throughput text", space_after=8)
        for k, (metric, label) in enumerate((("write_median", "Write"), ("read_median", "Read"))):
            cd = CategoryChartData(number_format="0.0")
            cd.categories = [w.capitalize() for w in WORKLOADS]
            for g in GROUPS:
                vals = [next((x[metric] for x in groups if x["group"] == g and x["workload"] == w),
                             None) for w in WORKLOADS]
                cd.add_series(GROUP_LABEL[g], [v if v is not None else 0 for v in vals])
            x = RIGHT_X + k * 3.9
            chart_header(s, f"{label} throughput (MB/s)", "median of all passing cycles",
                         x=x, w=3.7)
            gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(x - 0.1), Inches(1.85),
                                    Inches(3.85), Inches(4.3), cd)
            gf.name = f"{label.lower()} chart"
            ch = gf.chart
            quiet_chart(ch)
            plot = ch.plots[0]
            plot.gap_width = 70
            plot.overlap = -8
            for ser, g in zip(plot.series, GROUPS):
                ser.format.fill.solid()
                ser.format.fill.fore_color.rgb = GROUP_COLOR[g]
                ser.format.line.fill.background()
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.number_format = "0.0"
            dl.number_format_is_linked = False
            dl.position = XL_LABEL_POSITION.OUTSIDE_END
            dl.font.size = Pt(10)
            dl.font.color.rgb = BODY
        return s

    def _throughput_text(self, groups):
        def med(g, w, m):
            return next((x[m] for x in groups if x["group"] == g and x["workload"] == w), None)

        lines = []
        s8w, a8w = med("S8", "large", "write_median"), med("A8", "large", "write_median")
        s8r, a8r = med("S8", "large", "read_median"), med("A8", "large", "read_median")
        if s8w and a8w:
            faster = "ABLAZE" if a8w > s8w else "SanDisk"
            ratio = max(a8w, s8w) / min(a8w, s8w)
            lines.append(f"Writing large files, {faster} 8 GB drives were {ratio:.1f}× faster "
                         f"({a8w:.1f} vs {s8w:.1f} MB/s for ABLAZE vs SanDisk).")
        if s8r and a8r:
            faster = "ABLAZE" if a8r > s8r else "SanDisk"
            lines.append(f"Reading, {faster} was faster ({max(a8r, s8r):.1f} vs "
                         f"{min(a8r, s8r):.1f} MB/s).")
        small = [(g, med(g, "small", "write_median")) for g in GROUPS]
        if all(v for _, v in small):
            lo, hi = min(v for _, v in small), max(v for _, v in small)
            lines.append(f"Small files cut every drive's write throughput to {lo:.1f}–{hi:.1f} "
                         "MB/s, because each file is flushed before the next one starts.")
        return lines

    def cycle_time(self):
        drives = self.r["per_drive"]
        feas = {f["group"]: f for f in self.r["feasibility"]}
        s = self.new_slide("12", "Cycles Completed", notes=(
            "Cycles each drive completed by the stop. The plan expected about 24 cycles per "
            "8 GB drive and 12 per 16 GB drive at the planning speeds; the measured cycle "
            "times explain the difference. The pilot drives S8-01 and A8-01 ran 2 h 16 min "
            "longer than the others."))
        lines = []
        for g in GROUPS:
            f = feas.get(g)
            if f and f["measured_mean"]:
                lines.append([(f"{GROUP_LABEL[g]}: ", {"bold": True, "color": NAVY}),
                              (f"{f['measured_mean']:.2f} h per cycle on average "
                               f"(planned {f['planned_mean']:.2f} h)", {})])
        text_box(s, LEFT_X, self.body_y, LEFT_W, 3.2, lines, name="cycle text", space_after=8)
        chart_header(s, "Cycles completed by the stop, per drive",
                     "pilot drives S8-01 and A8-01 started 2 h 16 min earlier")
        order = [d for g in GROUPS for d in drives if d["group"] == g]
        cd = CategoryChartData(number_format="0")
        cd.categories = [d["drive_id"] for d in order]
        cd.add_series("Cycles", [d["cycles"] for d in order])
        gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(RIGHT_X - 0.1), Inches(1.85),
                                Inches(RIGHT_W + 0.1), Inches(4.0), cd)
        gf.name = "cycles chart"
        ch = gf.chart
        quiet_chart(ch, legend=False)
        ch.has_title = False
        bars_top_down(ch)
        plot = ch.plots[0]
        plot.gap_width = 60
        ser = plot.series[0]
        for i, d in enumerate(order):
            pt = ser.points[i]
            pt.format.fill.solid()
            pt.format.fill.fore_color.rgb = GROUP_COLOR[d["group"]]
            pt.format.line.fill.background()
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.number_format = "0"
        dl.number_format_is_linked = False
        dl.position = XL_LABEL_POSITION.OUTSIDE_END
        dl.font.size = Pt(11)
        dl.font.color.rgb = BODY
        planned8 = feas.get("S8", {}).get("planned_cycles_24h")
        planned16 = feas.get("A16", {}).get("planned_cycles_24h")
        if planned8 and planned16:
            note(s, f"Plan: about {planned8:.0f} cycles per 8 GB drive and {planned16:.0f} per "
                    "16 GB drive in 24 h.", 5.95)
        return s

    def phases(self):
        """Where the time of a small-file cycle went: write, read-back, delete, per drive."""
        small = [p for p in self.r["phases"] if p["workload"] == "small"]
        ph = {p["drive_id"]: p for p in small if p.get("logged", True)}
        unlogged = {p["drive_id"]: p for p in small if not p.get("logged", True)}
        s = self.new_slide("13", "Where the Time Went", notes=(
            "Each bar is one drive's small-file cycle of median duration, split into its three "
            "timed phases. The plan budgeted about 2.2 hours for a small-file cycle of an 8 GB "
            "drive and 4.4 hours for a 16 GB drive, and no time for deleting. Writing and "
            "deleting many small files decided how many cycles a drive completed."))
        def span(rows, key):
            lo, hi = min(p[key] for p in rows), max(p[key] for p in rows)
            return f"{lo:.1f}" if f"{lo:.1f}" == f"{hi:.1f}" else f"{lo:.1f}–{hi:.1f}"

        def hours(rows, key):  # "1.4 h", or "under a minute" for A8-01's instant deletes
            if max(p[key] for p in rows) < 1 / 60:
                return "under a minute"
            return f"{span(rows, key)} h"

        lines = []
        for g in GROUPS:
            rows = [p for p in ph.values() if p["group"] == g and p.get("outcome") == "pass"]
            # a drive that passed no small-file cycle is shown with the failed one
            failed = sorted((p for p in ph.values()
                             if p["group"] == g and p.get("outcome") != "pass"),
                            key=lambda p: p["drive_id"])
            parts = []
            if rows:
                parts.append(f"{span(rows, 'total_h')} h per cycle, of which writing "
                             f"{hours(rows, 'write_h')} and deleting {hours(rows, 'delete_h')}")
            if failed:
                parts.append(f"{and_list([p['drive_id'] for p in failed])} deleted for "
                             f"{span(failed, 'delete_h')} h, until they failed"
                             if len(failed) > 1 else
                             f"{failed[0]['drive_id']} deleted for "
                             f"{failed[0]['delete_h']:.1f} h, until it failed")
            lost = sorted(d for d, p in unlogged.items() if p["group"] == g)
            if lost:  # failed in a cycle whose phase times died with its worker
                parts.append(f"{and_list(lost)} failed as well; "
                             f"{'its' if len(lost) == 1 else 'their'} phase times were not logged")
            if parts:
                lines.append([(f"{GROUP_LABEL[g]}: ", {"bold": True, "color": NAVY}),
                              ("; ".join(parts), {})])
        text_box(s, LEFT_X, self.body_y, LEFT_W, 3.6, lines, name="phases text", space_after=8)
        chart_header(s, "Small-file cycle: hours in each phase",
                     "each drive's small-file cycle of median duration"
                     + ("; the failed one where none passed"
                        if any(p.get("outcome") != "pass" for p in ph.values()) else ""))
        order = [d["drive_id"] for g in GROUPS for d in self.r["per_drive"]
                 if d["group"] == g and d["drive_id"] in ph]
        cd = CategoryChartData(number_format="0.0")
        cd.categories = order
        for key in PHASES:
            cd.add_series(PHASE_LABEL[key], [ph[d][f"{key}_h"] for d in order])
        cd.add_series("total", [0 for _ in order])  # zero length: carries the total label
        gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_STACKED, Inches(RIGHT_X - 0.1), Inches(1.85),
                                Inches(RIGHT_W + 0.1), Inches(4.0), cd)
        gf.name = "phases chart"
        ch = gf.chart
        quiet_chart(ch)
        ch.has_title = False
        bars_top_down(ch)
        top = max(ph[d]["total_h"] for d in order)
        ch.value_axis.minimum_scale = 0
        unit = 2 if top <= 10 else 4
        ch.value_axis.maximum_scale = unit * math.ceil(top * 1.12 / unit)
        ch.value_axis.major_unit = unit
        ch.value_axis.tick_labels.number_format = "0"
        ch.value_axis.tick_labels.number_format_is_linked = False
        plot = ch.plots[0]
        plot.gap_width = 60
        plot.overlap = 100
        for ser, key in zip(plot.series, PHASES):
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = PHASE_COLOR[key]
            ser.format.line.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)  # gap between segments
            ser.format.line.width = Pt(1.0)
        total = plot.series[len(PHASES)]
        total.format.fill.background()
        total.format.line.fill.background()
        for i, d in enumerate(order):
            failed = ph[d].get("outcome") != "pass"
            dl = total.points[i].data_label
            dl.position = XL_LABEL_POSITION.INSIDE_BASE
            dl.text_frame.text = f"{ph[d]['total_h']:.1f} h" + (", failed" if failed else "")
            run = dl.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(11)
            run.font.color.rgb = CRIMSON if failed else MUTED
        # Hide the helper series from the legend.
        legend = ch.legend._element
        entry = legend.makeelement(qn("c:legendEntry"), {})
        entry.append(legend.makeelement(qn("c:idx"), {"val": str(len(PHASES))}))
        entry.append(legend.makeelement(qn("c:delete"), {"val": "1"}))
        legend.insert(1, entry)  # right after c:legendPos
        planned = {f["group"]: f["planned_small"] for f in self.r["feasibility"]}
        note(s, f"Plan: {planned['S8']:.1f} h per small-file cycle of an 8 GB drive and "
                f"{planned['A16']:.1f} h of a 16 GB drive, with no time for deleting.", 5.95)
        return s

    def life_data(self, s):
        """Proposal slide 8 (planned bound) becomes the measured life data."""
        r = self.r
        bounds = {b["set"]: b for b in r["bounds"]}
        pooled = bounds.get("All drives", {})
        for sh in s.shapes:
            if sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.TITLE:
                sh.text_frame.paragraphs[0].runs[0].text = "Life Data"
            if sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.BODY:
                sh.text_frame.paragraphs[0].runs[0].text = "14"
        failed = len(self._failed())
        set_shape_text(s, "stat 0", f"{failed} of {r['drives']}")
        set_shape_text(s, "stat label 0", "drives failed in 24 h")
        sandisk = bounds.get("S8", {})
        # the fit of the smallest group that holds every hard failure
        groups = {d["group"] for d in self._failed()}
        fit_set = next((g for g in ("A8", "A16") if groups == {g}
                        and bounds.get(g, {}).get("mle_scale") is not None), "ABLAZE")
        brand, brand_label = bounds.get(fit_set, {}), GROUP_LABEL.get(fit_set, fit_set)
        if brand.get("mle_scale") is not None and not sandisk.get("failures"):
            # with failures in one brand only: its fitted life against the other's bound
            set_shape_text(s, "stat 1", f"θ = {brand['mle_scale']:.1f}")
            hi = brand["mle_scale_hi"]
            set_shape_text(s, "stat label 1", f"cycles, {brand_label} Weibull fit "
                           f"(90 %: {brand['mle_scale_lo']:.1f}–{hi:.1f})" if hi < 10 else
                           f"cycles, {brand_label} Weibull fit "
                           f"(90 %: {brand['mle_scale_lo']:.1f}–{hi:.0f})")
            set_shape_text(s, "stat 2", f"θ ≥ {sandisk['theta_lower_g2']:.0f}")
            set_shape_text(s, "stat label 2", "cycles, SanDisk (no failure), 90 %, γ = 2")
        else:
            set_shape_text(s, "stat 1", f"θ ≥ {pooled.get('theta_lower_g2', float('nan')):.0f}")
            set_shape_text(s, "stat label 1", "cycles, 90 % confidence, γ = 2")
            set_shape_text(s, "stat 2", f"≥ {pooled.get('fdw_lower_g2', float('nan')):.0f}")
            set_shape_text(s, "stat label 2", "full-drive writes, same bound")
        set_shape_text(s, "chart title", "Lower bound on the characteristic life θ")
        set_shape_text(s, "chart sub", "Weibayes bound, 90 % confidence, all eight drives pooled")
        shapes = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
        measured = [self._weibayes_pooled(g) for g in shapes]
        planned = [self._weibayes_planned(g) for g in shapes]
        chart = next(sh for sh in s.shapes if sh.has_chart).chart
        cd = CategoryChartData(number_format="0")
        cd.categories = [f"{g:.1f}" for g in shapes]
        cd.add_series("This test", measured)
        cd.add_series("Planned (one day, no failure)", planned)
        chart.replace_data(cd)
        top = max(max(measured), max(planned))
        step = 10 if top <= 80 else 20 if top <= 160 else 40
        chart.value_axis.maximum_scale = step * math.ceil(top * 1.15 / step)
        chart.value_axis.minimum_scale = 0
        chart.value_axis.major_unit = step
        chart.category_axis.axis_title.text_frame.text = "Assumed Weibull shape γ"
        for p in chart.category_axis.axis_title.text_frame.paragraphs:
            for run in p.runs:
                run.font.size = Pt(12)
                run.font.color.rgb = MUTED
        ser = chart.plots[0].series[1]
        ser.format.line.color.rgb = RGBColor(0xAE, 0xB5, 0xBE)
        ser.format.line.width = Pt(1.75)
        ser.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        ser.marker.style = XL_MARKER_STYLE.NONE
        ser.smooth = False
        chart.has_legend = True
        chart.legend.position = XL_LEGEND_POSITION.TOP
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(11)
        chart.legend.font.color.rgb = BODY
        for ser in list(chart.plots[0].series)[1:]:
            ser.data_labels.show_value = False
        at2 = pooled.get("theta_lower_g2", float("nan"))
        lead = "No drive failed" if failed == 0 else (
            "One drive failed" if failed == 1 else
            f"{NUMBER_WORDS.get(failed, failed)} drives failed")
        fit = ""
        if pooled.get("mle_scale") is not None:  # two or more failures: the Weibull fit
            fit = (f" Weibull fit, all drives: θ = {pooled['mle_scale']:.0f} cycles "
                   f"({pooled['mle_scale_lo']:.0f}–{pooled['mle_scale_hi']:.0f}, 90 %).")
        set_shape_text(s, "chart note",
                       f"{lead}: θ ≥ {at2:.0f} cycles at γ = 2, against {planned[2]:.0f} "
                       "planned for one day without failures." + fit)
        if failed < 2:
            notes = ("With fewer than two hard failures the maximum-likelihood fit does not "
                     "exist, so the test gives the Weibayes lower bound on the characteristic "
                     "life. ")
        else:
            notes = ("With two or more failures the maximum-likelihood Weibull fit exists; the "
                     "planned Weibayes bound is still shown, as the plan prescribed. ")
            if brand.get("mle_scale") is not None:
                notes += (f"For the {brand_label} drives the fitted characteristic life is "
                          f"{brand['mle_scale']:.1f} cycles. ")
        s.notes_slide.notes_text_frame.text = (
            f"{lead}. {notes}Pooling all eight drives, the characteristic life exceeds "
            f"{at2:.0f} cycles with 90 percent confidence for a Weibull shape of 2; the line "
            "shows how the bound changes with the assumed shape.")

    def _weibayes_pooled(self, shape):
        from scipy import stats
        t = [d["t_hard"] for d in self.r["per_drive"] if d["cycles"] > 0]
        r = sum(1 for d in self.r["per_drive"] if d["hard_failed"])
        return (2 * sum(x ** shape for x in t) / stats.chi2.ppf(0.90, 2 * r + 2)) ** (1 / shape)

    def _weibayes_planned(self, shape):
        from scipy import stats
        feas = {f["group"]: f["planned_cycles_24h"] for f in self.r["feasibility"]}
        t = [feas[d["group"]] for d in self.r["per_drive"]]
        return (2 * sum(x ** shape for x in t) / stats.chi2.ppf(0.90, 2)) ** (1 / shape)

    def _forensics(self) -> dict[str, str]:
        """The ``\\resdef`` values of ``forensics.tex`` (from ``forensics.py``), if present."""
        path = self.results / "forensics.tex"
        if not path.exists():
            return {}
        return dict(re.findall(r"\\resdef\{([^}]*)\}\{([^}]*)\}",
                               path.read_text(encoding="utf-8")))

    def what_failed(self):
        """The raw reads of the failed drives (``fig_forensics`` of ``forensics.py``)."""
        pic = self.results / "fig_forensics.png"
        f = self._forensics()
        if not pic.exists() or not f:
            return None
        self.extra = 1  # the slides after this one move up by one number
        flips = sorted(int(v) for k, v in f.items() if k.endswith("-flip") and int(v) > 0)
        lost = [d for d in self.r["per_drive"] if d["files_missing"] > 0]
        lost_txt = and_list([f"{d['files_missing']:,}" for d in
                             sorted(lost, key=lambda d: d["files_missing"])])
        s = self.new_slide("15", "What Failed", notes=(
            "Each failed drive was read raw, with a read-only handle, and compared with what "
            "the file system and the test had written there. On the 16 GB drives one folder's "
            "directory held random bytes from the 8 KiB mark on, and every file with an entry "
            "there was lost. On the 8 GB drives the first eight clusters of the volume, which "
            "hold the allocation bitmap, the directories, and the identity file, came back with "
            "bits flipped in both directions, while the stale test data next to them had not "
            "one wrong bit. These are the records that every new or deleted file rewrites."
            + (" " + self.what_failed_note if self.what_failed_note else "")))
        flip_txt = (f"{flips[0]}–{flips[-1]} %" if len(flips) > 1 else
                    f"{flips[0]} %" if flips else "")
        text_box(s, RIGHT_X, 1.08, RIGHT_W, 0.9, [
            [("Every failure struck the file system's own records, never the test files. ",
              {"size": 13, "color": BODY}),
             (f"(a) 16 GB: random bytes in a folder's directory hid {lost_txt} files. "
              f"(b) 8 GB: the first 256 KiB came back with {flip_txt} of the bits flipped; "
              "the test data next to it had no wrong bit.", {"size": 13, "color": MUTED})]],
            name="what failed text")
        s.shapes.add_picture(str(pic), Inches(0.75), Inches(2.25), width=Inches(11.83))
        if self.what_failed_note:
            note(s, self.what_failed_note, 6.3, x=0.75, w=11.83)
        return s

    extra = 0
    what_failed_note = ""

    def degradation(self):
        r = self.r
        s = self.new_slide(f"{15 + self.extra}", "Warning Signs", notes=(
            "Write throughput of every passing cycle divided by the drive's own baseline for "
            "that file size. A drive that wears out would drift down toward the 50 percent "
            "slowdown threshold (D1); a drive whose trend is significantly negative is listed "
            "in the report."))
        sig = r.get("significant_declines") or []
        lines = [[(f"{r['slow_events']}", {"bold": True, "color": NAVY}),
                  (" slowdowns (D1) below 50 % of baseline", {})],
                 [(f"{r['transient_errors']}", {"bold": True, "color": NAVY}),
                  (" transient errors (D3) cleared by a retry", {})],
                 [(f"{r['cycle_failures']}", {"bold": True, "color": NAVY}),
                  (" cycle failures, soft or hard", {})],
                 [(f"{len(sig)}", {"bold": True, "color": NAVY}),
                  (" drive-workload trends that fall significantly (95 %)", {})]]
        text_box(s, LEFT_X, self.body_y, LEFT_W, 3.6, lines, name="warning text", space_after=10)
        chart_header(s, "Write throughput relative to each drive's own baseline",
                     "every passing cycle, each file size against its own baseline")
        paths = json.loads((self.results / "paths.json").read_text(encoding="utf-8"))
        cd = XyChartData()
        order = [d for g in GROUPS for d in sorted(paths) if paths[d]["group"] == g]
        for d in order:
            ser = cd.add_series(d)
            for k, y in zip(paths[d]["cycle"], paths[d]["relative"]):
                ser.add_data_point(k, y)
        kmax = max((max(p["cycle"]) for p in paths.values() if p["cycle"]), default=1)
        th = cd.add_series("D1 threshold (50 %)")
        th.add_data_point(0, 0.5)
        th.add_data_point(kmax + 1, 0.5)
        gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES, Inches(RIGHT_X - 0.1),
                                Inches(2.2), Inches(RIGHT_W + 0.1), Inches(3.85), cd)
        gf.name = "degradation chart"
        ch = gf.chart
        quiet_chart(ch, legend=False)
        ch.has_title = False
        va, ca = ch.value_axis, ch.category_axis
        va.minimum_scale, va.maximum_scale, va.major_unit = 0.0, 1.4, 0.25
        va.tick_labels.number_format = '0%'
        va.tick_labels.number_format_is_linked = False
        ca.minimum_scale, ca.maximum_scale = 0, kmax + 1
        ca.has_major_gridlines = False
        style_axis_text(ca, 11, MUTED)
        for ser, d in zip(ch.plots[0].series, order + ["threshold"]):
            if d == "threshold":
                ser.format.line.color.rgb = CRIMSON
                ser.format.line.width = Pt(1.25)
                ser.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
                ser.marker.style = XL_MARKER_STYLE.NONE
                continue
            g = paths[d]["group"]
            ser.format.line.color.rgb = GROUP_COLOR[g]
            ser.format.line.width = Pt(1.25)
            if paths[d]["dock"]:
                ser.format.line.dash_style = MSO_LINE_DASH_STYLE.SQUARE_DOT
            ser.marker.style = GROUP_MARKER[g]
            ser.marker.size = 6
            ser.marker.format.fill.solid()
            ser.marker.format.fill.fore_color.rgb = GROUP_COLOR[g]
            ser.marker.format.line.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
            ser.smooth = False
        self.legend_row(s, RIGHT_X, 1.80, [
            *[("line", GROUP_COLOR[g], GROUP_LABEL[g], False) for g in GROUPS],
            ("line", RGBColor(0x7C, 0x85, 0x92), "on the dock", True),
            ("line", CRIMSON, "D1 threshold", True)])  # the axis shows it at 50 %
        return s

    def legend_row(self, s, x, y, items):
        """A one-row legend of short line keys, for charts whose series repeat colors."""
        for kind, color, label, dashed in items:
            key = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x), Inches(y + 0.11),
                                         Inches(x + 0.32), Inches(y + 0.11))
            key.name = f"legend key {label}"
            key.line.color.rgb = color
            key.line.width = Pt(2.0)
            if dashed:
                key.line.dash_style = MSO_LINE_DASH_STYLE.SQUARE_DOT
            width = 0.085 * len(label) + 0.12  # Arial 11 pt
            text_box(s, x + 0.4, y, width, 0.25, [[(label, {"size": 11, "color": BODY})]],
                     name=f"legend {label}", spacing=1.0)
            x += 0.4 + width + 0.12

    def answers(self):
        r = self.r
        failed = self._failed()
        s = self.new_slide(f"{16 + self.extra}", "Findings", notes=(
            "What the one-day test can and cannot say. "
            + ("Life comparisons rest on censored data with no failures, so they are bounds, "
               "not estimates; " if not failed else
               "Life comparisons rest on few drives per group, so the fits have wide intervals "
               "and brand and lot cannot be separated; ")
            + "the performance comparisons are measured on every cycle."))
        text_box(s, LEFT_X, self.body_y, LEFT_W, 0.3, [[("Primary response", {"size": 12, "bold": True,
                                                                        "color": MUTED})]],
                 name="resp label")
        rest = NUMBER_WORDS.get(r["drives"] - len(failed), "").lower()
        # drives that failed a cycle but passed the check after it (intermittent, D2)
        soft = sorted(d["drive_id"] for d in r["per_drive"]
                      if not d["hard_failed"] and d.get("cycle_failures", 0) > 0)
        if not failed:
            resp = "No drive reached a hard failure: all eight are right-censored at the stop."
        elif len(failed) == 1:
            resp = (f"One drive failed ({failed[0]['drive_id']}, cycle {failed[0]['cycles']}); "
                    "the other seven are right-censored at the stop.")
        else:  # grouped by cycle, so that five failures still fit the box
            by_cycle: dict[int, list[str]] = {}
            for d in failed:
                by_cycle.setdefault(int(d["cycles"]), []).append(d["drive_id"])
            resp = (f"{NUMBER_WORDS.get(len(failed), len(failed))} drives failed hard: "
                    + "; ".join(f"{and_list(sorted(v))} at cycle {c}"
                                for c, v in sorted(by_cycle.items())) + ".")
            if soft:
                resp += (f" {and_list(soft)} failed a cycle but worked again"
                         f"{' (intermittent)' if len(soft) > 1 else ''}.")
            resp += f" The other {rest} are right-censored." if not soft else \
                " All others are right-censored."
        long_text = len(failed) > 2 or soft
        text_box(s, LEFT_X, self.body_y + 0.35, 3.4, 2.8 if long_text else 1.6, [[(
            resp, {"size": 16 if long_text else 20, "font": "Georgia", "color": NAVY})]],
            name="resp", spacing=1.15)
        items = self.answer_items or [
            ("Manufacturer", "See the report."), ("Capacity", "See the report."),
            ("File size", "See the report."), ("Warning signs", "See the report.")]
        for i, (head, body) in enumerate(items):
            y = 1.0 + i * 1.3
            text_box(s, RIGHT_X, y, 0.6, 0.6, [[(str(i + 1), {"size": 28, "font": "Georgia",
                                                             "color": CRIMSON})]],
                     name=f"rq n{i}", spacing=1.0)
            text_box(s, 5.70, y + 0.05, 6.88, 0.35, [[(head, {"size": 17, "bold": True,
                                                              "color": NAVY})]],
                     name=f"rq h{i}")
            text_box(s, 5.70, y + 0.45, 6.88, 0.75, [[(body, {"size": 14})]], name=f"rq t{i}",
                     spacing=1.15)
            if i < len(items) - 1:
                rule(s, 5.70, y + 1.22, 6.88, f"rq rule {i}")
        return s

    answer_items: list[tuple[str, str]] = []
    limit_items: tuple[list[str], list[str]] = ([], [])

    def limits(self):
        s = self.new_slide(f"{17 + self.extra}", "Limitations and Next Steps", notes=(
            "One day is a screening test: it rules out weak drives and measures performance, "
            "but the endurance of a typical drive needs a longer test. The software resumes "
            "from its logs, so a longer stop time is the only change needed."))
        text_box(s, LEFT_X, self.body_y, LEFT_W, 2.4, [
            "One day of cycling screens for weak drives; it cannot reach the wear-out range of "
            "typical drives."], name="limits text")
        left, right = self.limit_items
        for c, (head, items, color) in enumerate((("Limitations", left, NAVY),
                                                  ("Next steps", right, CRIMSON))):
            x = RIGHT_X + c * 3.95
            text_box(s, x, 1.05, 3.6, 0.4, [[(head, {"size": 17, "bold": True, "color": color})]],
                     name=f"col h{c}")
            for i, item in enumerate(items):
                y = 1.63 + i * 0.95
                rule(s, x, y, 3.6, f"col rule {c}{i}")
                text_box(s, x, y + 0.12, 3.6, 0.8, [[(item, {"size": 14})]],
                         name=f"col item {c}{i}", spacing=1.15)
        return s


def build(proposal: Path, results: Path, out: Path, answers: list, limits: tuple,
          what_failed_note: str = "") -> Path:
    deck = Deck(proposal, results)
    Deck.answer_items = answers
    Deck.limit_items = limits
    Deck.what_failed_note = what_failed_note
    old = list(deck.prs.slides)  # 0 cover ... 7 duration, 8 analysis, 9 schedule, 10 thanks
    deck.restyle()
    deck.cover()
    deck.schedule(old[9])
    deck.analysis_plan(old[8])
    deck.life_data(old[7])
    new = [deck.glance(), deck.timeline(), deck.throughput(), deck.cycle_time(), deck.phases()]
    raw = [s for s in [deck.what_failed()] if s is not None]  # needs forensics.py's figure
    tail = [deck.degradation(), deck.answers(), deck.limits()]
    deck.order(old[:7] + [old[9], old[8]] + new + [old[7]] + raw + tail + [old[10]])
    out.parent.mkdir(parents=True, exist_ok=True)
    deck.prs.save(str(out))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--text", help="JSON file with 'answers' [[head, body], ...], "
                                   "'limits' {'limitations': [...], 'next_steps': [...]}, and "
                                   "an optional 'what_failed_note'")
    args = ap.parse_args(argv)
    answers, limits, wf_note = [], ([], []), ""
    if args.text:
        t = json.loads(Path(args.text).read_text(encoding="utf-8-sig"))
        answers = [tuple(x) for x in t.get("answers", [])]
        limits = (t.get("limits", {}).get("limitations", []),
                  t.get("limits", {}).get("next_steps", []))
        wf_note = t.get("what_failed_note", "")
    path = build(Path(args.proposal), Path(args.results), Path(args.out), answers, limits,
                 wf_note)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
