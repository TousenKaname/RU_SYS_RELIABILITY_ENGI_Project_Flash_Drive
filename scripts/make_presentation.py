"""The project presentation: one ten-minute deck for the plan, the test, and its results.

    python scripts/make_presentation.py --template ../Proposal/ISE585_Project4_Proposal_v1.pptx \
        --results ../Project/results --text ../Project/deck_text.json \
        --hands data/fall2026/posttest-hands.json \
        --out ../Proposal/ISE585_Project4_Presentation.pptx

The course replaced the separate proposal and final presentations by one presentation of
ten minutes. This deck keeps the design of the proposal deck (its theme, the COVER and
CONTENT layouts, Georgia titles, Arial text, and the group colors of the report's figures)
and tells the whole study on ten content slides of about one minute each, in the order of
a paper: background, method, results, discussion. Each content slide states its message
in a one-sentence lead under the title; the evidence below it is a native chart, a table,
or a report figure drawn for the slide (``slide_figures.py``). The numbers come from
``results.json`` and the post-test log, and the speaker notes carry the talk.
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import re
import sys
from pathlib import Path

from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.enum.chart import XL_MARKER_STYLE, XL_TICK_MARK
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.packuri import PackURI
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_final_deck import (BODY, CRIMSON, GRIDLINE, GROUP_COLOR, GROUP_LABEL,  # noqa: E402
                             GROUPS, MUTED, NAVY, PHASE_COLOR, PHASE_LABEL, PHASES, RECOLOR,
                             RULE, bars_top_down, recolor_xml, text_box)

TINT = RGBColor(0xF2, 0xF4, 0xF7)   # the theme's lt2: surface of panels and cards
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
INK = RGBColor(0x22, 0x22, 0x22)
SPARE = RGBColor(0xC9, 0xCE, 0xD4)
TRACK = MUTED                       # the progress tracker: the steps not yet current
LIGHT = RGBColor(0xDD, 0xE3, 0xEA)  # text on the navy cover: body
LIGHT_MUTED = RGBColor(0xA9, 0xB4, 0xC2)  # ... secondary
ACCENT_LIGHT = RGBColor(0xF0, 0xA6, 0xAE)  # ... the accent, lightened for contrast
CHIP_GRAY = RGBColor(0x5F, 0x68, 0x75)    # verdict chip of an open answer
COVER_SPLIT = 7.55                  # inches: the cover layout's light right panel starts here
SECTIONS = ("Background", "Method", "Results", "Discussion", "Conclusion")
X0, W = 0.75, 11.83                 # left margin and text width of every slide
TOP = 2.35                          # first line of the body, under the lead
#: Type sizes (pt): one scale for the whole deck.
TITLE, LEAD, TEXT, SMALL, NOTE = 34, 18, 15, 14, 12
AXIS, DATA_LABEL = 13, 12
NO_STYLE = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # PowerPoint's "No Style, No Grid"
FOOTER_OLD, FOOTER_NEW = "Project 4 proposal", "Endurance of USB flash drives"
SLIDE_FIGURES = "slides"            # sub-folder of --results written by slide_figures.py
WORDS = {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
         7: "seven", 8: "eight"}

#: IEEE style: the proceedings, journal, site, or book title in italics.
REFERENCES = [
    [("S. Boboila and P. Desnoyers, “Write endurance in flash drives: Measurements and "
      "analysis,” in ", False),
     ("Proc. 8th USENIX Conf. File and Storage Technologies (FAST)", True), (", 2010.", False)],
    [("R. Templeman and A. Kapadia, “GANGRENE: Exploring the mortality of flash memory,” "
      "in ", False), ("Proc. 7th USENIX Workshop on Hot Topics in Security (HotSec)", True),
     (", 2012.", False)],
    [("G. Lui, “Experiment: 8Gb USB flash drive endurance test,” ", False),
     ("Gough’s Tech Zone", True), (" (blog), May 2017.", False)],
    [("JEDEC, “Solid-state drive (SSD) requirements and endurance test method,” JEDEC "
      "Standard JESD218C.01, 2026.", False)],
    [("E. L. Kaplan and P. Meier, “Nonparametric estimation from incomplete "
      "observations,” ", False), ("J. Amer. Statist. Assoc.", True),
     (", vol. 53, no. 282, pp. 457–481, 1958.", False)],
    [("W. Q. Meeker, L. A. Escobar, and F. G. Pascual, ", False),
     ("Statistical Methods for Reliability Data", True),
     (", 2nd ed. Hoboken, NJ, USA: Wiley, 2022.", False)],
]


# -- drawing helpers ----------------------------------------------------------------------
def lum(rgb: RGBColor) -> float:
    """WCAG relative luminance, to choose ink or white text on a fill."""
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def text_on(fill: RGBColor) -> RGBColor:
    return INK if lum(fill) > 0.28 else WHITE


def box(slide, x, y, w, h, name, fill=TINT, shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06,
        line=None):
    """A filled panel without outline (or with ``line`` as its outline color)."""
    sp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    sp.name = name
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        sp.adjustments[0] = radius
    sp.fill.solid()
    sp.fill.fore_color.rgb = fill
    if line is None and fill == TINT:  # every tinted panel and card: one hairline outline
        line = RULE
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line
        sp.line.width = Pt(0.75)
    sp.shadow.inherit = False
    tf = sp.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    return sp


def pill(slide, x, y, w, h, label, fill, name, size=13):
    sp = box(slide, x, y, w, h, name, fill=fill, radius=0.25)
    tf = sp.text_frame
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    r.font.size, r.font.bold, r.font.name = Pt(size), True, "Arial"
    r.font.color.rgb = text_on(fill)
    return sp


def line(slide, x1, y1, x2, y2, name, color=MUTED, width=1.25, arrow=False):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2),
                                   Inches(y2))
    c.name = name
    c.line.color.rgb = color
    c.line.width = Pt(width)
    if arrow:
        ln = c.line._get_or_add_ln()
        ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))
    return c


def hairline(slide, x, y, w, name):
    return line(slide, x, y, x + w, y, name, color=RULE, width=0.75)


def heading(slide, x, y, w, title, sub=None, name="heading"):
    """The bold heading over a chart, table, or list, with an optional muted subtitle."""
    text_box(slide, x, y, w, 0.32, [[(title, {"size": SMALL, "bold": True, "color": NAVY})]],
             name=f"{name} title", spacing=1.0)
    if sub:
        text_box(slide, x, y + 0.33, w, 0.3, [[(sub, {"size": NOTE, "color": MUTED})]],
                 name=f"{name} sub", spacing=1.0)


def caption(slide, x, y, w, text, name="caption", h=0.4):
    return text_box(slide, x, y, w, h, [[(text, {"size": NOTE, "italic": True, "color": MUTED})]],
                    name=name, spacing=1.1)


def _border(tcPr, tag, color=None, width=0.0):
    ln = tcPr.makeelement(qn(f"a:{tag}"), {"w": str(int(width * 12700)), "cap": "flat",
                                          "cmpd": "sng", "algn": "ctr"})
    if color is None:
        ln.append(ln.makeelement(qn("a:noFill"), {}))
    else:
        fill = ln.makeelement(qn("a:solidFill"), {})
        fill.append(fill.makeelement(qn("a:srgbClr"), {"val": str(color)}))
        ln.append(fill)
        ln.append(ln.makeelement(qn("a:prstDash"), {"val": "solid"}))
    return ln


def table(slide, x, y, widths, rows, name, *, head_h=0.4, row_h=0.46, size=SMALL,
          head_size=NOTE, align=None, bold_first=False):
    """A table in the proposal's style: muted bold header, navy rule under it, hairlines.

    Each cell is a string or a list of (text, options) runs; ``align`` gives 'l' or 'r'
    per column. PowerPoint's "No Style, No Grid" removes the default banded fills.
    """
    n, m = len(rows), len(widths)
    gf = slide.shapes.add_table(n, m, Inches(x), Inches(y), Inches(sum(widths)),
                                Inches(head_h + row_h * (n - 1)))
    gf.name = name
    tbl = gf.table
    tblPr = tbl._tbl.tblPr
    for attr in ("firstRow", "bandRow"):
        tblPr.set(attr, "0")
    sid = tblPr.find(qn("a:tableStyleId"))
    if sid is None:
        sid = tblPr.makeelement(qn("a:tableStyleId"), {})
        tblPr.append(sid)
    sid.text = NO_STYLE
    for j, wd in enumerate(widths):
        tbl.columns[j].width = Inches(wd)
    align = align or "l" * m
    for i, row in enumerate(rows):
        tbl.rows[i].height = Inches(head_h if i == 0 else row_h)
        for j, value in enumerate(row):
            cell = tbl.cell(i, j)
            cell.margin_left = 0
            cell.margin_right = Inches(0.12)
            cell.margin_top = cell.margin_bottom = Inches(0.04)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.alignment = PP_ALIGN.RIGHT if align[j] == "r" else PP_ALIGN.LEFT
            runs = [(value, {})] if isinstance(value, str) else value
            for text, opts in runs:
                r = p.add_run()
                r.text = text
                f = r.font
                f.name = "Arial"
                if i == 0:
                    f.size, f.bold, f.color.rgb = Pt(head_size), True, MUTED
                else:
                    f.size = Pt(opts.get("size", size))
                    f.bold = opts.get("bold", bold_first and j == 0)
                    f.italic = opts.get("italic", False)
                    f.color.rgb = opts.get("color", NAVY if (bold_first and j == 0) else BODY)
            tcPr = cell._tc.get_or_add_tcPr()
            for child in list(tcPr):
                tcPr.remove(child)
            bottom = (NAVY, 1.0) if i == 0 else (RULE, 0.75)
            for tag, spec in (("lnL", None), ("lnR", None), ("lnT", None), ("lnB", bottom)):
                tcPr.append(_border(tcPr, tag, *(spec or (None, 0.0))))
            tcPr.append(tcPr.makeelement(qn("a:noFill"), {}))
    return gf


def chart_style(ch, legend=True):
    """One look for every chart: Arial, muted value axis, hairline grid, legend on top."""
    ch.font.name = "Arial"
    ch.font.size = Pt(AXIS)
    ch.font.color.rgb = BODY
    ch.has_title = False
    ch.has_legend = legend
    if legend:
        ch.legend.position = XL_LEGEND_POSITION.TOP
        ch.legend.include_in_layout = False
        ch.legend.font.size = Pt(AXIS)
        ch.legend.font.color.rgb = BODY
    va = ch.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = GRIDLINE
    va.major_gridlines.format.line.width = Pt(0.75)
    va.format.line.fill.background()
    va.major_tick_mark = XL_TICK_MARK.NONE
    va.tick_labels.font.size = Pt(AXIS)
    va.tick_labels.font.color.rgb = MUTED
    ca = ch.category_axis
    ca.format.line.color.rgb = RULE
    ca.major_tick_mark = XL_TICK_MARK.NONE
    ca.tick_labels.font.size = Pt(AXIS)
    ca.tick_labels.font.color.rgb = BODY


def axis_title(axis, text):
    axis.has_title = True
    tf = axis.axis_title.text_frame
    tf.text = text
    for p in tf.paragraphs:
        for r in p.runs:
            r.font.size, r.font.bold = Pt(AXIS), False
            r.font.color.rgb = MUTED


def hide_legend_entries(chart, indices):
    legend = chart.legend._element
    for k, idx in enumerate(indices):
        entry = legend.makeelement(qn("c:legendEntry"), {})
        entry.append(legend.makeelement(qn("c:idx"), {"val": str(idx)}))
        entry.append(legend.makeelement(qn("c:delete"), {"val": "1"}))
        legend.insert(1 + k, entry)  # right after c:legendPos


def picture(slide, path: Path, x, y, *, w=None, h=None, name, alt=""):
    pic = slide.shapes.add_picture(str(path), Inches(x), Inches(y),
                                   width=Inches(w) if w else None, height=Inches(h) if h else None)
    pic.name = name
    if alt:
        pic._element.nvPicPr.cNvPr.set("descr", alt)
    return pic


def fmt(x, nd=1):
    return f"{x:.{nd}f}"


def span(lo, hi, nd=1, sep="–"):
    a, b = fmt(lo, nd), fmt(hi, nd)
    return a if a == b else f"{a}{sep}{b}"


def ci(lo, hi):
    """A 90 % interval with two significant digits, as in the report's tables."""
    def two(v):
        return f"{v:.2g}" if v < 10 else f"{v:.0f}" if v >= 10.05 else f"{v:.1f}"
    return f"{two(lo)}–{two(hi)}"


# -- the deck -----------------------------------------------------------------------------
class Presentation10:
    def __init__(self, template: Path, results: Path, text: dict, hands: dict | None,
                 run: Path | None = None):
        self.prs = Presentation(str(template))
        self.results = results
        self.figs = results / SLIDE_FIGURES
        self.r = json.loads((results / "results.json").read_text(encoding="utf-8"))
        self.text = text
        self.hands = hands or {}
        self.run = run  # the campaign's run folder, for per-cycle values
        self.layout = next(l for l in self.prs.slide_layouts if l.name == "CONTENT")
        slides = list(self.prs.slides)
        self.number_sp = copy.deepcopy(next(
            sh for sh in slides[1].shapes if sh.is_placeholder
            and sh.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER)._element)
        photos = {sh.name: sh.image.blob for sh in slides[3].shapes if sh.shape_type == 13}
        self.photo_sandisk, self.photo_ablaze = photos["drives sandisk"], photos["drives ablaze"]
        self.cover, self.closing = slides[0], slides[-1]
        for s in slides[1:-1]:  # the proposal's content slides are rebuilt, not reused
            self.drop(s)
        # python-pptx names a new slide part after the slide count, which would reuse the
        # kept slides' part names; park them out of the way until build() renumbers all
        for k, s in enumerate((self.cover, self.closing)):
            s.part.partname = PackURI(f"/ppt/slides/slide{900 + k}.xml")
        self.per = {d["drive_id"]: d for d in self.r["per_drive"]}
        self.bounds = {b["set"]: b for b in self.r["bounds"]}

    # -- structure ----------------------------------------------------------------------
    def drop(self, slide):
        lst = self.prs.slides._sldIdLst
        for sld in list(lst):
            if int(sld.get("id")) == slide.slide_id:
                self.prs.part.drop_rel(sld.rId)
                lst.remove(sld)
                return

    def delete_seconds(self, drive: str, workload: str = "small") -> list[float]:
        """Delete times (s) of the passing cycles of one workload, from the drive's log."""
        path = self.run / drive / "cycles.csv" if self.run else None
        if not path or not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            return [float(r["delete_s"]) for r in csv.DictReader(f)
                    if r["workload"] == workload and r["outcome"] == "pass" and r["delete_s"]]

    def slide(self, section: str, title: str, lead: str | None, notes: str):
        """A CONTENT slide: progress tracker, title on one line, and the lead sentence.

        The placeholders keep the layout's type (Georgia title) and move to a full-width
        header, the same on every content slide.
        """
        s = self.prs.slides.add_slide(self.layout)
        sp = copy.deepcopy(self.number_sp)
        s.shapes._spTree.append(sp)
        sp.nvSpPr.cNvPr.id = max(int(e.get("id")) for e in s.shapes._spTree.iter()
                                 if e.tag.endswith("}cNvPr") and e.get("id")) + 1
        for ph in s.placeholders:
            kind = ph.placeholder_format.type
            tf = ph.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            if kind == PP_PLACEHOLDER.TITLE:
                ph.left, ph.top, ph.width, ph.height = (Inches(X0), Inches(0.80), Inches(W),
                                                        Inches(0.62))
                tf.text = title
                tf.paragraphs[0].runs[0].font.size = Pt(TITLE)
            elif kind == PP_PLACEHOLDER.BODY and section in SECTIONS:
                ph.left, ph.top, ph.width, ph.height = (Inches(X0), Inches(0.48), Inches(W),
                                                        Inches(0.28))
                self.tracker(tf.paragraphs[0], section)
        self.fit_number(sp)  # after the loop above, which zeroes every placeholder inset
        if lead:
            text_box(s, X0, 1.50, W, 0.7, [[(lead, {"size": LEAD, "color": BODY})]],
                     name="lead", spacing=1.1)
        s.notes_slide.notes_text_frame.text = notes
        return s

    def restyle(self):
        for layout in self.prs.slide_layouts:
            for shape in layout.shapes:
                if shape.has_text_frame:
                    for p in shape.text_frame.paragraphs:
                        for run in p.runs:
                            run.text = run.text.replace(FOOTER_OLD, FOOTER_NEW)
        theme = self.prs.slide_master.part.part_related_by(RT.THEME)
        xml = theme.blob.decode("utf-8")
        for old, new in RECOLOR.items():  # the report's validated group colors
            xml = xml.replace(f'<a:srgbClr val="{old}"/>', f'<a:srgbClr val="{new}"/>')
        theme._blob = xml.encode("utf-8")

    @staticmethod
    def tracker(p, section: str):
        """The talk's five parts in one line, the current one in the accent."""
        for k, name in enumerate(SECTIONS):
            if k:
                sep = p.add_run()
                sep.text = "   ·   "
                sep.font.size, sep.font.bold, sep.font.color.rgb = Pt(11), False, TRACK
            r = p.add_run()
            r.text = name.upper()
            r.font.size, r.font.bold = Pt(11), name == section
            r.font.color.rgb = CRIMSON if name == section else TRACK
            r.font._element.set("spc", "100")

    @staticmethod
    def fit_number(sp):
        """The slide number box: wide enough for two digits, flush right, no side insets."""
        xfrm = sp.find(qn("p:spPr")).find(qn("a:xfrm"))
        off, ext = xfrm.find(qn("a:off")), xfrm.find(qn("a:ext"))
        right = int(off.get("x")) + int(ext.get("cx"))
        width = int(Inches(0.8))
        off.set("x", str(right - width))
        ext.set("cx", str(width))
        body = sp.find(qn("p:txBody")).find(qn("a:bodyPr"))
        body.set("lIns", "0")
        body.set("rIns", "38100")  # 3 pt: room for the end of the paragraph, right-aligned

    def navy_panel(self, s, name):
        """The dark left half of the cover and the closing slide, behind everything else."""
        panel = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(COVER_SPLIT),
                                   self.prs.slide_height)
        panel.name = name
        panel.fill.solid()
        panel.fill.fore_color.rgb = NAVY
        panel.line.fill.background()
        panel.shadow.inherit = False
        tree = s.shapes._spTree
        tree.remove(panel._element)
        tree.insert(2, panel._element)  # after the group properties: the bottom of the stack

    def cover_slide(self):
        s = self.cover
        recolor_xml(s._element)
        self.navy_panel(s, "cover panel")
        for sh in s.shapes:
            if not sh.has_text_frame:
                continue
            title = sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.TITLE
            if title:
                sh.height = Inches(1.75)
            for i, p in enumerate(sh.text_frame.paragraphs):
                for r in p.runs:
                    r.text = r.text.replace("Project 4 proposal", "Project 4 presentation")
                    r.font.color.rgb = (WHITE if title else ACCENT_LIGHT if sh.name == "label"
                                        else LIGHT_MUTED if sh.name == "course" or i > 0
                                        else WHITE)
        ab = self.bounds["ABLAZE"]
        text_box(s, X0, 4.2, 6.3, 0.4, [[(
            f"{self.r['cycles_total']} cycles  ·  {self.r['tb_written']:.2f} TB  ·  "
            f"{ab['failures']} of {ab['drives']} ABLAZE drives failed",
            {"size": 16, "color": LIGHT})]], name="teaser", spacing=1.0)
        s.notes_slide.notes_text_frame.text = (
            "We are Guoan Wang, Shangqing Wei, and Mengtong Ai. In ten minutes we present "
            "Project 4: a life test of eight USB flash drives under repeated copy, verify, and "
            "delete cycling. We cover the plan, the 24-hour test we ran on October 23 and 24, "
            "and what it found.")

    def closing_slide(self):
        s = self.closing
        recolor_xml(s._element)
        self.navy_panel(s, "closing panel")
        for sh in list(s.shapes):
            if sh.is_placeholder and sh.has_text_frame and not sh.text_frame.text.strip():
                sh._element.getparent().remove(sh._element)  # an empty placeholder
                continue
            if not sh.has_text_frame:
                continue
            title = sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.TITLE
            if title:
                sh.height = Inches(0.8)
            for i, p in enumerate(sh.text_frame.paragraphs):
                for r in p.runs:
                    r.font.color.rgb = WHITE if title or i == 0 else LIGHT_MUTED
        qr = self.figs / "qr_repo.png"
        if qr.exists():
            box(s, X0, 4.55, 1.5, 1.5, "qr panel", fill=WHITE, radius=0.06)
            picture(s, qr, X0 + 0.1, 4.65, w=1.3, name="qr code",
                    alt="QR code of the project repository on GitHub")
            text_box(s, X0 + 1.8, 4.95, 4.6, 0.8, [
                [("Code, logs, and report", {"size": SMALL, "bold": True, "color": WHITE})],
                [("scan to open the GitHub repository", {"size": NOTE, "color": LIGHT_MUTED})]],
                name="qr caption", spacing=1.2)
        s.notes_slide.notes_text_frame.text = (
            "Thank you. The report, the logs of every cycle, and the test software are in the "
            "project repository. We are happy to take questions.")

    # -- background ---------------------------------------------------------------------
    def motivation(self):
        s = self.slide("Background", "Motivation and Research Questions",
                       "SSDs carry an endurance rating [4], while USB flash drives carry none and "
                       "are seldom tested.", notes=(
            "Flash cells survive only a limited number of program and erase cycles. Solid-state "
            "drives carry an endurance rating under JEDEC JESD218. USB flash drives carry none. "
            "The few published wear-out tests used three to five drives each. Their low-cost "
            "drives wore out, slowed down, or locked read-only after roughly 100 to 1,000 "
            "full-drive writes, and none of the studies fitted a life distribution. We asked "
            "four questions: does the manufacturer matter at the same capacity, does capacity "
            "matter within one brand, does the file size of the workload matter, and does "
            "anything warn before a drive fails. The response is the number of cycles to a "
            "hard failure of each drive."))
        heading(s, X0, TOP, 6.05, "Prior wear-out tests of USB drives", name="prior")
        rows = [["Study", "Drives", "Observed"],
                ["Boboila & Desnoyers [1]", "4 small drives", "2 wore out, 1 slowed down first"],
                ["Templeman & Kapadia [2]", "5 × 2 GB", "all read-only after 205 to 627 GB"],
                ["Lui [3]", "3 × 8 GB", "failed at 632, 959, and 9,751 cycles"]]
        table(s, X0, TOP + 0.45, [2.45, 1.35, 2.25], rows, "prior tests", row_h=0.66)
        text_box(s, X0, 5.3, 6.05, 1.1, [[(
            "Most low-cost drives failed after 100 to 1,000 full-drive writes, and none of "
            "these studies fitted a life distribution.",
            {"size": 17, "italic": True, "font": "Georgia", "color": NAVY})]],
            name="gap", spacing=1.15)
        x, w = 7.2, 5.38
        heading(s, x, TOP, w, "Research questions",
                "response: cycles to hard failure, T, of each drive", name="rq")
        items = [("Manufacturer", "Do SanDisk and ABLAZE drives of 8 GB differ in cycles "
                                  "to failure?"),
                 ("Capacity", "Do ABLAZE 16 GB drives last a different number of cycles "
                              "than 8 GB ones?"),
                 ("File size", "Do small, medium, or large files change the failure rate and "
                               "the slowdown?"),
                 ("Warning signs", "Which indicators appear before a hard failure, and how "
                                   "early?")]
        for i, (head, body) in enumerate(items):
            y = TOP + 0.75 + i * 0.92
            text_box(s, x, y - 0.02, 0.45, 0.5, [[(str(i + 1), {"size": 22, "bold": True,
                                                               "font": "Arial",
                                                               "color": CRIMSON})]],
                     name=f"rq n{i}", spacing=1.0)
            text_box(s, x + 0.55, y, w - 0.55, 0.3, [[(head, {"size": TEXT, "bold": True,
                                                              "color": NAVY})]], name=f"rq h{i}")
            text_box(s, x + 0.55, y + 0.31, w - 0.55, 0.52, [[(body, {"size": SMALL})]],
                     name=f"rq t{i}", spacing=1.05)
            if i < len(items) - 1:
                hairline(s, x + 0.55, y + 0.86, w - 0.55, f"rq rule {i}")
        return s

    # -- method -------------------------------------------------------------------------
    def drives_rig(self):
        s = self.slide("Method", "Drives and Test Rig",
                       "Eight drives from three purchase lots cycled in parallel on one Windows "
                       "desktop for 24 hours.", notes=(
            "We bought drives from three lots: two SanDisk Cruzer Blade 8 GB drives, four "
            "ABLAZE 8 GB drives, and two ABLAZE 16 GB drives, plus one more ABLAZE 8 GB drive "
            "as a spare. All eight ran at the same time on one Windows desktop, six in the "
            "USB ports of the desktop and two in a USB-C dock, each driven by its own worker "
            "process that logs every cycle. The spare stayed unplugged. After the test it "
            "checks every port that a failed drive used, so that a bad port is not counted as "
            "a bad drive."))
        widths = [0.85, 2.42, 0.9, 0.7, 0.75]
        pw = sum(widths)
        box(s, X0, TOP, pw, 1.7, "photo panel")
        for blob, px, alt in ((self.photo_sandisk, X0 + 0.5, "SanDisk Cruzer Blade drives"),
                              (self.photo_ablaze, X0 + 3.05, "ABLAZE metal keychain drives")):
            pic = s.shapes.add_picture(io.BytesIO(blob), Inches(px), Inches(TOP + 0.1),
                                       height=Inches(1.5))
            pic.name = "photo"
            pic._element.nvPicPr.cNvPr.set("descr", alt)
        rows = [["Group", "Drive", "Capacity", "Price", "Tested"],
                ["S8", "SanDisk Cruzer Blade", "8 GB", "$9.99", "2"],
                ["A8", "ABLAZE keychain, metal", "8 GB", "$4.40", "4 + 1"],
                ["A16", "ABLAZE keychain, metal", "16 GB", "$7.50", "2"]]
        y_tab, head_h, row_h = TOP + 1.95, 0.4, 0.46
        rows = [rows[0]] + [[[("      " + r[0], {"bold": True, "color": NAVY})], *r[1:]]
                            for r in rows[1:]]
        table(s, X0, y_tab, widths, rows, "drives table", head_h=head_h, row_h=row_h,
              align="lllrr")
        for i, g in enumerate(GROUPS):  # group swatch, the key to the colors of every chart
            y = y_tab + head_h + (i + 0.5) * row_h - 0.09
            box(s, X0, y, 0.18, 0.18, f"swatch {g}", fill=GROUP_COLOR[g],
                shape=MSO_SHAPE.RECTANGLE)
        caption(s, X0, y_tab + head_h + 3 * row_h + 0.1, pw,
                "+ 1: the spare A8-05, which checks suspect ports after the test.",
                name="spare note", h=0.3)
        self.rig(s)
        return s

    def rig(self, s):
        """The rig as a native diagram: host, six desktop ports, the dock and its two ports."""
        ports = [("W1", "S8-01"), ("W2", "A16-01"), ("W3", "A8-01"), ("W4", "A8-02"),
                 ("W5", "A8-03"), ("W6", "A8-04")]
        dock = [("D1", "S8-02"), ("D2", "A16-02")]
        group = {d: self.per[d]["group"] for d in self.per}
        hx, hy, hw, hh = 7.15, 3.25, 1.95, 1.15
        box(s, hx, hy, hw, hh, "host")
        text_box(s, hx + 0.18, hy + 0.17, hw - 0.3, 0.9, [
            [("Host W", {"size": TEXT, "bold": True, "color": NAVY})],
            [("Windows desktop", {"size": NOTE, "color": MUTED})],
            [("a worker per drive", {"size": NOTE, "color": MUTED})]],
            name="host text", spacing=1.15)
        bus_x, pill_x, pill_w, pill_h = 9.45, 10.2, 1.75, 0.36
        ys = [TOP + 0.18 + i * 0.5 for i in range(len(ports))]
        line(s, hx + hw, hy + hh / 2, bus_x, hy + hh / 2, "host link")
        line(s, bus_x, ys[0], bus_x, ys[-1], "bus")
        for (port, d), y in zip(ports, ys):
            line(s, bus_x, y, pill_x, y, f"wire {port}")
            text_box(s, bus_x + 0.12, y - 0.3, 0.5, 0.26, [[(port, {"size": NOTE,
                                                                "color": MUTED})]],
                     name=f"port {port}", spacing=1.0)
            pill(s, pill_x, y - pill_h / 2, pill_w, pill_h, d, GROUP_COLOR[group[d]],
                 f"drive {d}")
        dx, dy, dw, dh = 8.55, 5.5, 1.1, 0.85
        line(s, hx + hw / 2, hy + hh, hx + hw / 2, dy + dh / 2, "dock link v")
        line(s, hx + hw / 2, dy + dh / 2, dx, dy + dh / 2, "dock link h")
        box(s, dx, dy, dw, dh, "dock")
        text_box(s, dx, dy + 0.13, dw, 0.6, [
            [("Dock", {"size": SMALL, "bold": True, "color": NAVY})],
            [("USB-C", {"size": NOTE, "color": MUTED})]], name="dock text", align=PP_ALIGN.CENTER,
            spacing=1.1)
        for k, (port, d) in enumerate(dock):
            y = dy + 0.2 + k * 0.45
            line(s, dx + dw, y, pill_x, y, f"wire {port}")
            text_box(s, dx + dw + 0.1, y - 0.3, 0.5, 0.26, [[(port, {"size": NOTE,
                                                                 "color": MUTED})]],
                     name=f"port {port}", spacing=1.0)
            pill(s, pill_x, y - pill_h / 2, pill_w, pill_h, d, GROUP_COLOR[group[d]],
                 f"drive {d}")

    def cycle(self):
        s = self.slide("Method", "Test Cycle and Failure Criteria",
                       "Each cycle fills 90 % of the free space, reads every byte back around "
                       "the host cache, and deletes it.", notes=(
            "One cycle plans a workload, writes it, reads every byte back around the host "
            "cache, compares it byte by byte with data regenerated from a seed, and deletes "
            "it, checking that the free space returns. Each cycle fills 90 percent of the "
            "free space with one file size: a handful of large files, hundreds of medium "
            "files, or about 90,000 small files on an 8 GB drive, in rotation. A failed cycle "
            "triggers an automatic recovery check. A drive that fails again on a second port "
            "after the test, or fails three times in ten cycles, has failed hard, and its life "
            "T is that cycle. We estimate reliability with Kaplan and Meier, fit Weibull "
            "models by maximum likelihood, and use a Weibayes bound where no drive fails."))
        steps = [("Plan", "one file size per cycle, in rotation"),
                 ("Write", "host to drive, every file flushed"),
                 ("Read back", "drive to host, around the host cache"),
                 ("Compare", "byte by byte with regenerated data"),
                 ("Delete", "then check the free space")]
        cw, gap, ch = 2.05, 0.395, 1.05
        for i, (head, body) in enumerate(steps):
            x = X0 + i * (cw + gap)
            box(s, x, TOP, cw, ch, f"step {i}")
            text_box(s, x + 0.18, TOP + 0.13, cw - 0.3, 0.85, [
                [(f"{i + 1:02d}  ", {"size": NOTE, "bold": True, "color": CRIMSON}),
                 (head, {"size": TEXT, "bold": True, "color": NAVY})],
                [(body, {"size": NOTE, "color": BODY})]], name=f"step {i} text", spacing=1.12,
                space_after=2)
            if i < len(steps) - 1:
                line(s, x + cw + 0.07, TOP + ch / 2, x + cw + gap - 0.07, TOP + ch / 2,
                     f"step arrow {i}", arrow=True)
        caption(s, X0, TOP + ch + 0.08, W,
                "Repeated until the drive fails or the test stops. An error that two retries "
                "do not clear fails the cycle and starts a recovery check.",
                name="loop note", h=0.3)
        files = {}
        for p in self.r["phases"]:
            if p.get("files"):
                cap = 16 if p["group"] == "A16" else 8
                files.setdefault((p["workload"], cap), []).append(p["files"])

        def count(w, cap):
            v = files.get((w, cap), [])
            if not v:
                return "–"
            lo, hi = min(v), max(v)
            if w == "small":
                return f"≈ {round((lo + hi) / 2, -3):,.0f}"
            return f"{lo:,.0f} to {hi:,.0f}" if lo != hi else f"{lo:,.0f}"
        y0 = 4.0
        heading(s, X0, y0, 5.5, "Workloads", name="workloads")
        sizes = {"large": "512 MiB to 2 GiB", "medium": "1 to 64 MiB", "small": "4 to 256 KiB"}
        rows = [["Workload", "File sizes", "Files, 8 GB", "Files, 16 GB"]] + [
            [w.capitalize(), sizes[w], count(w, 8), count(w, 16)] for w in sizes]
        table(s, X0, y0 + 0.42, [1.25, 1.85, 1.15, 1.25], rows, "workloads table",
              row_h=0.42, align="llrr", bold_first=True)
        caption(s, X0, y0 + 0.42 + 0.4 + 3 * 0.42 + 0.14, 5.5,
                "Analysis: Kaplan–Meier [5], Weibull maximum likelihood with 90 % Wald "
                "intervals, and a Weibayes bound where no drive fails [6].",
                name="analysis note", h=0.5)
        x, w = 6.6, 5.98
        heading(s, x, y0, w, "Failure criteria", name="criteria")
        items = [("Hard failure: ", "fails again on a second port after the test, fails "
                                    "three times in ten cycles, locks read-only, or changes "
                                    "capacity. Its life T is that cycle.", 3),
                 ("Port fault: ", "the spare drive also fails on that port, so the failure is "
                                  "not counted.", 2),
                 ("Warning signs: ", "write speed below half of its own baseline, or a "
                                     "transient error.", 2)]
        y = y0 + 0.45
        for i, (head, body, lines) in enumerate(items):
            h = 0.08 + 0.255 * lines
            text_box(s, x, y, w, h, [[(head, {"size": SMALL, "bold": True, "color": NAVY}),
                                      (body, {"size": SMALL})]], name=f"criterion {i}",
                     spacing=1.08)
            y += h + 0.12
        return s

    # -- results ------------------------------------------------------------------------
    def timeline(self):
        r = self.r
        failed = [d for d in self.r["per_drive"] if d["hard_failed"]]
        s = self.slide("Results", "The 24-Hour Test as Run", None, notes=(
            "This is every cycle of the test. The two pilot drives started at 17:44 on "
            "Friday, the other six at 20:00, and everything stopped at 20:00 on Saturday. The "
            "color gives the file size, and the light-green small-file cycles are the long ones. "
            f"In 24 hours the drives completed {r['cycles_total']} cycles and wrote "
            f"{r['tb_written']:.2f} terabytes in {r['files_written'] / 1e6:.1f} million files, "
            "and every file that was found read back exactly as written. Five drives failed, "
            "in red: the three ABLAZE 8 GB drives in their first small-file cycle, and the two "
            "ABLAZE 16 GB drives in their second and third. The two SanDisk drives and one "
            "ABLAZE 8 GB drive were still running at the stop."))
        tiles = [(f"{r['cycles_total']}", "cycles in 24 hours", NAVY),
                 (f"{r['tb_written']:.2f} TB", "written to the drives", NAVY),
                 (f"{r['files_written'] / 1e6:.2f} M", "files verified byte by byte", NAVY),
                 (f"{len(failed)} of {r['drives']}", "drives failed hard", CRIMSON)]
        tw = W / len(tiles)
        for k, (value, label, color) in enumerate(tiles):
            x = X0 + k * tw
            text_box(s, x, 1.42, tw - 0.25, 0.55, [[(value, {"size": 26, "bold": True,
                                                             "font": "Arial",
                                                             "color": color})]],
                     name=f"tile {k}", spacing=1.0)
            text_box(s, x, 1.97, tw - 0.25, 0.28, [[(label, {"size": NOTE, "color": MUTED})]],
                     name=f"tile {k} label", spacing=1.0)
        pic = picture(s, self.figs / "slide_timeline.png", X0, 2.42, w=11.4, name="timeline",
                      alt="Every cycle of every drive from Friday 17:44 to Saturday 20:00")
        bottom = (pic.top + pic.height) / 914400
        caption(s, X0, bottom + 0.05, W,
                "All five failures came in small-file cycles. Each bar is one cycle, red bars "
                "failed, and hatched bars were cut short at the stop and are not counted.",
                name="timeline caption", h=0.3)
        return s

    def throughput(self):
        g = {(x["group"], x["workload"]): x for x in self.r["per_group_workload"]}
        s8, a8 = g[("S8", "large")], g[("A8", "large")]
        ratio = a8["write_median"] / s8["write_median"]
        s = self.slide("Results", "Throughput and Cycle Time",
                       f"ABLAZE wrote large files {ratio:.1f} times faster than SanDisk, and small "
                       "files slowed every drive.", notes=(
            "Performance differed in opposite directions. Writing large or medium files, the "
            f"ABLAZE 8 GB drives reached {a8['write_median']:.1f} MB/s, {ratio:.1f} times "
            f"the {s8['write_median']:.1f} MB/s of SanDisk. Reading, SanDisk was faster. With "
            "small files every drive slowed to about 1 MB/s or less, because each file is "
            "flushed before the next one starts. The right panel shows where the time of a "
            "small-file cycle went. SanDisk needed under two hours. A8-03 and A8-04 spent 11 "
            "to 14 hours deleting their 90,000 files, and then failed. A8-01, the survivor, "
            "deleted its files in seconds. We come back to that on slide 9."))
        heading(s, X0, TOP, 5.6, "Write throughput (MB/s)", "median of the passing cycles",
                name="write")
        cd = CategoryChartData(number_format="0.0")
        cd.categories = ["Large", "Medium", "Small"]
        for grp in GROUPS:
            cd.add_series(GROUP_LABEL[grp], [g[(grp, w)]["write_median"]
                                              for w in ("large", "medium", "small")])
        gf = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(X0),
                                Inches(TOP + 0.65), Inches(5.65), Inches(3.2), cd)
        gf.name = "write chart"
        ch = gf.chart
        chart_style(ch)
        plot = ch.plots[0]
        plot.gap_width, plot.overlap = 80, -10
        for ser, grp in zip(plot.series, GROUPS):
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = GROUP_COLOR[grp]
            ser.format.line.fill.background()
        plot.has_data_labels = True
        dl = plot.data_labels
        dl.number_format, dl.number_format_is_linked = "0.0", False
        dl.position = XL_LABEL_POSITION.OUTSIDE_END
        dl.font.size, dl.font.color.rgb = Pt(DATA_LABEL), BODY
        va = ch.value_axis
        va.minimum_scale, va.maximum_scale, va.major_unit = 0, 15, 5  # the report: 0-15 MB/s
        va.tick_labels.number_format, va.tick_labels.number_format_is_linked = "0", False
        caption(s, X0, 6.3, 5.6, f"Reading large files, SanDisk was faster: "
                                 f"{s8['read_median']:.1f} against {a8['read_median']:.1f} MB/s.",
                name="read note", h=0.3)
        x = 6.85
        heading(s, x, TOP, 5.73, "Small-file cycle: hours in each phase",
                "the cycle of median duration, or the failed cycle where none passed",
                name="phases")
        small = {p["drive_id"]: p for p in self.r["phases"]
                 if p["workload"] == "small" and p.get("logged", True) and p.get("total_h")}
        order = [d["drive_id"] for grp in GROUPS for d in self.r["per_drive"]
                 if d["group"] == grp and d["drive_id"] in small]
        cd = CategoryChartData(number_format="0.0")
        cd.categories = order
        for key in PHASES:
            cd.add_series(PHASE_LABEL[key], [small[d][f"{key}_h"] for d in order])
        cd.add_series("total", [0 for _ in order])  # zero length: carries the total label
        gf = s.shapes.add_chart(XL_CHART_TYPE.BAR_STACKED, Inches(x - 0.1), Inches(TOP + 0.65),
                                Inches(5.83), Inches(3.25), cd)
        gf.name = "phases chart"
        ch = gf.chart
        chart_style(ch)
        bars_top_down(ch)
        top = max(small[d]["total_h"] for d in order)
        va = ch.value_axis
        va.minimum_scale, va.major_unit = 0, 4
        va.maximum_scale = 4 * math.ceil(top * 1.22 / 4)
        va.tick_labels.number_format, va.tick_labels.number_format_is_linked = "0", False
        plot = ch.plots[0]
        plot.gap_width, plot.overlap = 55, 100
        for ser, key in zip(plot.series, PHASES):
            ser.format.fill.solid()
            ser.format.fill.fore_color.rgb = PHASE_COLOR[key]
            ser.format.line.color.rgb = WHITE  # the surface gap between segments
            ser.format.line.width = Pt(1.0)
        total = plot.series[len(PHASES)]
        total.format.fill.background()
        total.format.line.fill.background()
        for i, d in enumerate(order):
            failed = small[d].get("outcome") != "pass"
            lab = total.points[i].data_label
            lab.position = XL_LABEL_POSITION.INSIDE_BASE
            lab.text_frame.text = f"{small[d]['total_h']:.1f} h" + (", failed" if failed else "")
            run = lab.text_frame.paragraphs[0].runs[0]
            run.font.size = Pt(DATA_LABEL)
            run.font.color.rgb = CRIMSON if failed else MUTED
        hide_legend_entries(ch, [len(PHASES)])
        missing = [p["drive_id"] for p in self.r["phases"]
                   if p["workload"] == "small" and not p.get("logged", True)]
        if missing:
            caption(s, x, 6.3, 5.73, f"{' and '.join(missing)}: phase times not logged, "
                                     "because its worker was stopped.", name="phases note",
                    h=0.3)
        return s

    def life(self):
        b = self.bounds
        ab, a8, a16, s8 = b["ABLAZE"], b["A8"], b["A16"], b["S8"]
        s = self.slide("Results", "Failures and Life Data",
                       "Five of six ABLAZE drives failed within nine cycles, and neither SanDisk "
                       "drive failed at all.", notes=(
            "Here are the life data. The Kaplan-Meier estimate falls to 0.25 for the ABLAZE "
            "8 GB drives at cycle 3 and to zero for the ABLAZE 16 GB drives at cycle 9. The "
            "SanDisk line stays at one, censored at 23 and 26 cycles. Fitted by maximum "
            f"likelihood, the six ABLAZE drives have a Weibull shape of {ab['mle_shape']:.1f} "
            f"and a characteristic life of {ab['mle_scale']:.1f} cycles, with a 90 percent "
            f"interval of {ab['mle_scale_lo']:.1f} to {ab['mle_scale_hi']:.1f}: a rising "
            "hazard, as wear-out would give. For SanDisk, without failures, the Weibayes bound "
            f"says the characteristic life is at least {s8['theta_lower_g2']:.0f} cycles. After "
            "the test, all five failed drives failed again on a second port, and the spare "
            "passed on all eight ports, so none of these failures was a port fault."))
        heading(s, X0, TOP, 6.5, "Kaplan–Meier reliability by group [5]", name="km")
        theta, gamma = ab["mle_scale"], ab["mle_shape"]
        self.legend_keys(s, X0, TOP + 0.42, [("solid", GROUP_COLOR[g], GROUP_LABEL[g])
                                             for g in GROUPS], name="km legend 1")
        self.legend_keys(s, X0, TOP + 0.74, [
            ("dash", NAVY, f"Weibull fit, ABLAZE (θ = {theta:.1f}, γ = {gamma:.1f})"),
            ("plus", MUTED, "censored")], name="km legend 2")
        self.km_chart(s, X0, TOP + 1.05, 6.65, 3.2, ab)
        x = 7.75
        heading(s, x, TOP, 4.83, "Weibull fit, maximum likelihood [6]", "90 % intervals",
                name="mle")
        rows = [["Group", "Failed", "Shape γ", "Scale θ (cycles)"]]
        for label, f, n in (("ABLAZE 8 GB", a8, 4), ("ABLAZE 16 GB", a16, 2),
                            ("ABLAZE, all", ab, 6)):
            rows.append([label, f"{f['failures']} of {n}", f"{f['mle_shape']:.1f}",
                         f"{f['mle_scale']:.1f} ({ci(f['mle_scale_lo'], f['mle_scale_hi'])})"])
        rows.append(["SanDisk 8 GB", "0 of 2", "–", f"≥ {s8['theta_lower_g2']:.0f} *"])
        table(s, x, TOP + 0.72, [1.68, 0.85, 0.85, 1.45], rows, "mle table", row_h=0.44,
              align="lrrr", bold_first=True)
        caption(s, x, TOP + 0.72 + 0.4 + 4 * 0.44 + 0.08, 4.83,
                "* Weibayes 90 % lower bound for γ = 2: no SanDisk drive failed.",
                name="weibayes note", h=0.3)
        text_box(s, x, 5.85, 4.83, 0.8, [[
            ("Post-test check: ", {"size": SMALL, "bold": True, "color": NAVY}),
            ("all five failed again on a second port, and the spare passed on all eight ports, "
             "so no failure was a port fault.", {"size": SMALL})]], name="posttest", spacing=1.1)
        return s

    def legend_keys(self, s, x, y, items, name="legend"):
        """A one-row legend of short line keys (or a + glyph), for a chart drawn without one."""
        for k, (kind, color, label) in enumerate(items):
            if kind == "plus":
                text_box(s, x, y - 0.07, 0.3, 0.34, [[("+", {"size": 18, "bold": True,
                                                             "color": color})]],
                         name=f"{name} key {k}", spacing=1.0)
                kx = x + 0.28
            else:
                key = line(s, x, y + 0.13, x + 0.4, y + 0.13, f"{name} key {k}", color=color,
                           width=2.25 if kind == "solid" else 1.5)
                if kind == "dash":
                    key.line.dash_style = MSO_LINE_DASH_STYLE.DASH
                kx = x + 0.5
            width = 0.1 * len(label) + 0.15  # Arial 13 pt, with slack
            text_box(s, kx, y, width, 0.28, [[(label, {"size": AXIS, "color": BODY})]],
                     name=f"{name} {k}", spacing=1.0)
            x = kx + width + 0.2

    def km_chart(self, s, x, y, w, h, fit):
        """Kaplan-Meier steps per group, censoring marks, and the ABLAZE Weibull curve."""
        steps = {g: [] for g in GROUPS}
        for k in self.r["km_steps"]:
            if k["set"] in steps:
                steps[k["set"]].append((k["cycle"], k["reliability"]))
        drives = self.r["per_drive"]
        cd = XyChartData()
        end = {g: max(d["cycles"] for d in drives if d["group"] == g) for g in GROUPS}
        for g in GROUPS:  # step function: horizontal, then vertical at each failure
            ser = cd.add_series(GROUP_LABEL[g])
            r_now, pts = 1.0, [(0, 1.0)]
            for t, r_new in sorted(steps[g]):
                pts += [(t, r_now), (t, r_new)]
                r_now = r_new
            last = max(end[g], pts[-1][0])
            if r_now > 0:
                pts.append((last, r_now))
            for px, py in pts:
                ser.add_data_point(px, py)
        theta, gamma = fit["mle_scale"], fit["mle_shape"]
        wb = cd.add_series("Weibull fit, ABLAZE")
        for i in range(0, 65):
            t = i * 0.25
            wb.add_data_point(t, math.exp(-((t / theta) ** gamma)))
        cens = {g: [] for g in GROUPS}  # a drive still working at the stop
        for d in drives:
            if not d["hard_failed"]:
                r_at = 1.0
                for t, rr in sorted(steps[d["group"]]):
                    if t <= d["cycles"]:
                        r_at = rr
                cens[d["group"]].append((d["cycles"], r_at))
        cens_series = []
        for g in GROUPS:
            if cens[g]:
                ser = cd.add_series(f"censored {g}")
                for px, py in cens[g]:
                    ser.add_data_point(px, py)
                cens_series.append(g)
        gf = s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES_NO_MARKERS, Inches(x), Inches(y),
                                Inches(w), Inches(h), cd)
        gf.name = "km chart"
        ch = gf.chart
        chart_style(ch, legend=False)
        va, ca = ch.value_axis, ch.category_axis
        va.minimum_scale, va.maximum_scale, va.major_unit = 0, 1.0, 0.25
        va.tick_labels.number_format, va.tick_labels.number_format_is_linked = "0.00", False
        ca.minimum_scale, ca.maximum_scale, ca.major_unit = 0, 28, 4
        ca.has_major_gridlines = False
        ca.tick_labels.font.color.rgb = MUTED
        axis_title(ca, "Cycles")
        axis_title(va, "Reliability")
        series = list(ch.plots[0].series)
        for ser, g in zip(series[:3], GROUPS):
            ser.format.line.color.rgb = GROUP_COLOR[g]
            ser.format.line.width = Pt(2.25)
            ser.marker.style = XL_MARKER_STYLE.NONE
            ser.smooth = False
        wbs = series[3]
        wbs.format.line.color.rgb = NAVY
        wbs.format.line.width = Pt(1.5)
        wbs.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        wbs.marker.style = XL_MARKER_STYLE.NONE
        wbs.smooth = True
        for ser, g in zip(series[4:], cens_series):
            ser.format.line.fill.background()
            ser.marker.style = XL_MARKER_STYLE.PLUS
            ser.marker.size = 11
            ser.marker.format.line.color.rgb = GROUP_COLOR[g]
            ser.marker.format.line.width = Pt(1.75)
            ser.smooth = False
        return gf

    def what_failed(self):
        f = dict(re.findall(r"\\resdef\{([^}]*)\}\{([^}]*)\}",
                            (self.results / "forensics.tex").read_text(encoding="utf-8")))
        flips = sorted(int(v) for k, v in f.items() if k.endswith("-flip") and int(v) > 0)
        lost = sorted(d["files_missing"] for d in self.r["per_drive"] if d["files_missing"])
        s = self.slide("Results", "What Failed: The Records of the File System",
                       "Every failure struck the records that each new or deleted file rewrites, "
                       "never the test data around them.", notes=(
            "We read the failed drives raw, read-only, and compared every byte with what the "
            "file system and the test had written. On the 16 GB drives, the directory cluster "
            "of one folder held random bytes from the 8 KiB mark on, and every file with an "
            f"entry there was lost, {lost[0]} and {lost[-1]} files. The folder can no longer "
            "be deleted. On the 8 GB drives, the first eight clusters of the volume, which "
            "hold the allocation bitmap, the directories, and the identity file, came back "
            f"with {flips[0]} to {flips[-1]} percent of their bits flipped in both directions, "
            "while the test data right next to them had no wrong bit. These are the records "
            "that every new or deleted file rewrites: the hot spot of a small-file workload."))
        pic = picture(s, self.figs / "slide_forensics.png", X0, TOP - 0.05, w=W,
                      name="forensics", alt="Raw reads of the failed drives")
        y = (pic.top + pic.height) / 914400 + 0.22
        text_box(s, X0, y, 5.6, 0.8, [[
            ("(a) ABLAZE 16 GB: ", {"size": SMALL, "bold": True, "color": NAVY}),
            (f"random bytes in one directory hid {lost[0]} and {lost[-1]} files, and the "
             "folder cannot be deleted.", {"size": SMALL})]], name="caption a", spacing=1.1)
        text_box(s, 6.75, y, 5.83, 0.8, [[
            ("(b) ABLAZE 8 GB: ", {"size": SMALL, "bold": True, "color": NAVY}),
            (f"the first 256 KiB had {flips[0]} to {flips[-1]} % of its bits flipped, while "
             "the test data beside it was intact.", {"size": SMALL})]], name="caption b",
            spacing=1.1)
        return s

    # -- discussion ---------------------------------------------------------------------
    def port_w3(self):
        obs = self.hands.get("observed", {})
        spare = {v["port"]: v["removal_policy"] for v in obs.values() if v["drive"] == "SPARE"}
        a801 = {v["port"]: v["removal_policy"] for v in obs.values() if v["drive"] == "A8-01"}
        others = sorted(p for p, pol in spare.items() if p != "W3" and pol == 3)
        dock = sorted(p for p, pol in a801.items() if p != "W3" and pol == 3)
        small = {p["drive_id"]: p for p in self.r["phases"] if p["workload"] == "small"}
        slow = [small[d]["delete_h"] for d in ("A8-03", "A8-04") if small.get(d, {}).get("delete_h")]
        fast = self.delete_seconds("A8-01") or [small["A8-01"]["delete_h"] * 3600]
        lo, hi = math.floor(min(fast)), math.ceil(max(fast))
        s = self.slide("Discussion", "Why A8-01 Survived: Port W3",
                       "Windows treats port W3 as built in, so it cached the file-system writes "
                       "of the surviving A8-01.", notes=(
            "Why did A8-01 survive while its three siblings failed? Its port. The firmware "
            "describes port W3 as not visible to the user, so Windows treats any drive in it "
            "as a built-in disk and caches its file-system writes. That is why A8-01 deleted "
            f"its 90,000 files in {lo} to {hi} seconds, while A8-03 and A8-04 took 11 to 14 "
            "hours, writing every directory and bitmap update to the drive at once. We "
            "confirmed it after the test: the spare drive got the fixed-disk policy in W3 and "
            "the quick-removal policy in all seven other ports, and A8-01 got quick removal on "
            "the dock. So the survival of A8-01 is confounded with its port."))
        heading(s, X0, TOP, 6.45, "Removal policy that Windows assigned",
                "read for every drive on every port after the test", name="policy")
        fixed = [("expect no removal: ", {"bold": True, "color": CRIMSON}),
                 ("writes cached", {})]
        quick = "quick removal: written at once"
        rows = [["Drive", "On port W3", "On the other ports"],
                ["A8-01 (survived)", fixed,
                 f"{quick} ({'dock port ' + ', '.join(dock) if dock else 'dock'})"],
                ["Spare A8-05", fixed, f"{quick} (all {len(others)})"],
                ["Other seven", "–", quick]]
        table(s, X0, TOP + 0.72, [1.75, 2.05, 2.65], rows, "policy table", row_h=0.62,
              bold_first=True)
        text_box(s, X0, TOP + 0.72 + 0.4 + 3 * 0.62 + 0.2, 6.45, 0.8, [[
            ("Cause: ", {"size": SMALL, "bold": True, "color": NAVY}),
            ("the ACPI record of the physical location of each port marks W3 as not visible "
             "to the user.", {"size": SMALL})]], name="cause", spacing=1.1)
        x = 7.85
        heading(s, x, TOP, 4.73, "Deleting the files of one small-file cycle", name="delete")
        stats = [(f"{lo} to {hi} s" if lo != hi else f"{lo} s", "A8-01 on W3, which survived",
                  NAVY),
                 (f"{span(min(slow), max(slow), 0, sep=' to ')} h" if slow else "–",
                  "A8-03 and A8-04, which then failed", CRIMSON)]
        for i, (value, label, color) in enumerate(stats):
            yy = TOP + 0.55 + i * 1.45
            text_box(s, x, yy + 0.04, 4.73, 0.75, [[(value, {"size": 36, "bold": True,
                                                             "font": "Arial",
                                                             "color": color})]],
                     name=f"stat {i}", spacing=1.0)
            text_box(s, x, yy + 0.78, 4.73, 0.4, [[(label, {"size": SMALL, "color": MUTED})]],
                     name=f"stat {i} label", spacing=1.0)
        text_box(s, x, 5.6, 4.73, 0.9, [[(
            "The survival of A8-01 is thus confounded with its port, so a longer test must "
            "give every drive the same policy.", {"size": SMALL, "italic": True, "color": BODY})]],
            name="implication", spacing=1.1)
        return s

    def answers(self):
        items = [tuple(a) for a in self.text.get("answers", [])]
        verdicts = [tuple(v) for v in self.text.get("verdicts", [])]
        s = self.slide("Conclusion", "Answers to the Research Questions",
                       "One day of cycling separated the brands, and each failure struck records "
                       "that small files rewrite.", notes=(
            "To answer our questions. Manufacturer: all five failures were ABLAZE drives. If "
            "brand did not matter, the chance of that would be under one percent. Capacity: "
            "the fitted lives of the two ABLAZE lots overlap, so capacity changed how the "
            "drives failed, not clearly when. File size: every failure came in a small-file "
            "cycle, a 0.3 percent chance if file size did not matter, and it hit the records "
            "that each file rewrites. Warning signs: none. No slowdown, transient error, or "
            "earlier failed cycle came first, and only re-reading the file-system records "
            "found the damage early."))
        cw, chh, gx, gy = 5.79, 1.98, 0.25, 0.2
        for i, (head, body) in enumerate(items[:4]):
            x = X0 + (i % 2) * (cw + gx)
            y = TOP + (i // 2) * (chh + gy)
            box(s, x, y, cw, chh, f"answer card {i}")
            text_box(s, x + 0.25, y + 0.18, 0.55, 0.6, [[(str(i + 1), {
                "size": 24, "bold": True, "font": "Arial", "color": CRIMSON})]],
                name=f"answer n{i}",
                spacing=1.0)
            text_box(s, x + 0.85, y + 0.24, 2.0, 0.35, [[(head, {
                "size": 16, "bold": True, "color": NAVY})]], name=f"answer h{i}", spacing=1.0)
            if i < len(verdicts):
                verdict, kind = verdicts[i]
                vw = 0.098 * len(verdict) + 0.4  # Arial 12 pt bold, with padding
                pill(s, x + cw - 0.25 - vw, y + 0.22, vw, 0.36, verdict,
                     CRIMSON if kind == "finding" else CHIP_GRAY, f"verdict {i}", size=NOTE)
            text_box(s, x + 0.85, y + 0.7, cw - 1.25, 1.2, [[(body, {"size": SMALL})]],
                     name=f"answer t{i}", spacing=1.12)
        return s

    def limits(self):
        s = self.slide("Discussion", "Limitations and Next Steps",
                       "One day screens out weak drives, while earlier tests saw failures only "
                       "after about 100 full-drive writes.", notes=(
            "Limitations. One day reached at most 22 full-drive writes, far below where "
            "earlier tests saw drives fail, so the SanDisk drives are only shown to last more "
            "than 23 cycles, not how long they last. Each group came from one lot with two to "
            "four drives, so brand, lot, and capacity cannot be separated. And the ports were "
            "not equal. Next, we would test more ABLAZE lots for longer, re-read the "
            "file-system records every cycle, give every drive the same removal policy, and "
            "count the file-system writes per cycle. With these limits in mind, here are our "
            "answers."))
        heading(s, X0, TOP, 6.4, "Data written by each drive, against earlier tests [2], [3]",
                name="context")
        pic = picture(s, self.figs / "slide_context.png", X0, TOP + 0.42, h=3.8,
                      name="context", alt="Full-drive writes per drive against earlier failures")
        x = (pic.left + pic.width) / 914400 + 0.45
        w = 12.58 - x
        fdw_max = max(d["full_drive_writes"] for d in self.r["per_drive"])
        cols = [("Limitations", NAVY, [
                    f"One day reached at most {fdw_max:.0f} full-drive writes.",
                    "Two to four drives per group, one lot each.",
                    "Unequal ports: W3 cached the writes of A8-01.",
                    "The operator, not the software, found A8-02."]),
                ("Next steps", CRIMSON, [
                    "Test ABLAZE drives from other lots, for longer.",
                    "Re-read the file-system records every cycle.",
                    "Give every drive the same removal policy."])]
        y = TOP
        for c, (head, color, items) in enumerate(cols):
            text_box(s, x, y, w, 0.32, [[(head, {"size": SMALL, "bold": True, "color": color})]],
                     name=f"col h{c}", spacing=1.0)
            y += 0.42
            for i, item in enumerate(items):
                hairline(s, x, y, w, f"col rule {c}{i}")
                text_box(s, x, y + 0.09, w, 0.32, [[(item, {"size": SMALL})]],
                         name=f"col item {c}{i}", spacing=1.0)
                y += 0.46
            y += 0.32
        return s

    def references(self):
        s = self.slide("References", "References", None, notes=(
            "The sources cited on the slides. The report lists the full bibliography."))
        for ph in list(s.placeholders):  # the title says it: no section label here
            if ph.placeholder_format.type == PP_PLACEHOLDER.BODY:
                ph._element.getparent().remove(ph._element)
        paras = [[(f"[{i + 1}]\t", {"size": SMALL, "color": MUTED}),
                  *[(text, {"size": SMALL, "italic": it}) for text, it in ref]]
                 for i, ref in enumerate(REFERENCES)]
        tb = text_box(s, X0, 1.75, W, 4.8, paras, name="reference list", spacing=1.12,
                      space_after=10)
        for p in tb.text_frame.paragraphs:  # hanging indent past the number
            pPr = p._p.get_or_add_pPr()
            pPr.set("marL", str(int(Inches(0.55))))
            pPr.set("indent", str(-int(Inches(0.55))))
        return s

    # -- build --------------------------------------------------------------------------
    def build(self, out: Path) -> Path:
        self.restyle()
        self.cover_slide()
        self.closing_slide()
        body = [self.motivation(), self.drives_rig(), self.cycle(), self.timeline(),
                self.throughput(), self.life(), self.what_failed(), self.port_w3(),
                self.limits(), self.answers(), self.references()]
        lst = self.prs.slides._sldIdLst
        ids = {s.slide_id: el for s, el in zip(self.prs.slides, list(lst))}
        wanted = [ids[s.slide_id] for s in [self.cover, *body, self.closing]]
        for el in list(lst):
            lst.remove(el)
        for el in wanted:
            lst.append(el)
        for i, s in enumerate(self.prs.slides, 1):  # part names in the order of the deck
            s.part.partname = PackURI(f"/ppt/slides/slide{i}.xml")
            for fld in s._element.iter(qn("a:fld")):  # the number cached for other viewers
                if fld.get("type") == "slidenum" and fld.find(qn("a:t")) is not None:
                    fld.find(qn("a:t")).text = str(i)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(out))
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--template", required=True, help="the proposal deck (design source)")
    ap.add_argument("--results", required=True, help="make_results.py output folder")
    ap.add_argument("--text", required=True, help="deck_text.json with the answers")
    ap.add_argument("--hands", help="posttest-hands.json of the post-test protocol")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    text = json.loads(Path(args.text).read_text(encoding="utf-8-sig"))
    hands = (json.loads(Path(args.hands).read_text(encoding="utf-8-sig"))
             if args.hands and Path(args.hands).exists() else None)
    run = Path(args.hands).resolve().parent if args.hands else None
    out = Presentation10(Path(args.template), Path(args.results), text, hands, run).build(
        Path(args.out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
