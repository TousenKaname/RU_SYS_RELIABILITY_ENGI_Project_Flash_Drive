"""The proposal talk: the test plan in about fifteen minutes, written to be read aloud.

    python scripts/make_proposal_deck.py --template ../Proposal/ISE585_Project4_Proposal_v1.pptx \
        --figures ../Project/figures/slides --qr ../Project/results/slides/qr_repo.png \
        --out ../Proposal/ISE585_Project4_Proposal.pptx

The deck walks through the test plan (``Project/main.tex``) in its own order and adds
nothing that the plan does not say: the introduction and research questions, the
literature review subsection by subsection with Table I, then every figure and table of
the setup, the procedure, and the design. The analysis plan and the risks are left for the
final presentation. The deck uses the design of the presentation (``make_presentation.py``):
the first proposal deck as the template, a navy cover, a progress tracker, and a
one-sentence lead under every title. The slide text is written in full sentences, so a
presenter can read it, and the speaker notes hold the same script without citation marks. The figures are the plan's own
(``make_plan_figures.py``), drawn again at slide size, and the tables are native.
"""

from __future__ import annotations

import argparse
import copy
import io
import math
import re
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib as mpl  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.enum.shapes import MSO_SHAPE, PP_PLACEHOLDER  # noqa: E402
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN  # noqa: E402
from pptx.opc.constants import RELATIONSHIP_TYPE as RT  # noqa: E402
from pptx.opc.packuri import PackURI  # noqa: E402
from pptx.oxml.ns import qn  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_final_deck import (BODY, CRIMSON, GREEN, GROUP_COLOR, MUTED, NAVY,  # noqa: E402
                             RECOLOR, RULE, recolor_xml)
from make_presentation import (ACCENT_LIGHT, LIGHT, LIGHT_MUTED, NOTE, SMALL, TEXT,  # noqa: E402
                               TINT, TITLE, TOP, WHITE, W, X0, Presentation10, box,
                               hairline, heading, line, picture, table)

SECTIONS = ("Background", "Method", "Design")
RIGHT = X0 + W                      # right margin
BOTTOM = 6.72                       # last line of the body, above the footer
EQ_DPI = 300
MONO = "Consolas"

#: IEEE style, venue in italics; numbered in the order of first citation on the slides.
REFS = {
    "cai2017error": [("Y. Cai, S. Ghose, E. F. Haratsch, Y. Luo, and O. Mutlu, “Error "
                      "characterization, mitigation, and recovery in flash-memory-based "
                      "solid-state drives,” ", False), ("Proc. IEEE", True),
                     (", vol. 105, no. 9, pp. 1666–1704, 2017.", False)],
    "grupp2012bleak": [("L. M. Grupp, J. D. Davis, and S. Swanson, “The bleak future of NAND "
                        "flash memory,” in ", False),
                       ("Proc. 10th USENIX Conf. File and Storage Technologies (FAST)", True),
                       (", 2012.", False)],
    "hu2009write": [("X.-Y. Hu, E. Eleftheriou, R. Haas, I. Iliadis, and R. Pletka, “Write "
                     "amplification analysis in flash-based solid state drives,” in ", False),
                    ("Proc. SYSTOR", True), (", 2009, pp. 1–9.", False)],
    "jedec2026jesd218": [("JEDEC, “Solid-state drive (SSD) requirements and endurance test "
                          "method,” JEDEC Standard JESD218C.01, 2026.", False)],
    "cox2010jedec": [("A. Cox, “JEDEC SSD specifications explained,” JEDEC Solid State Drive "
                      "Tutorial, San Jose, CA, USA, 2010.", False)],
    "jedec2022jesd219": [("JEDEC, “Solid-state drive (SSD) endurance workloads,” JEDEC "
                          "Standard JESD219A.01, 2022.", False)],
    "kim2023automating": [("J. Kim, M. Jeon, S. Jang, and H. Oh, “Automating endurance test "
                           "for flash-based storage devices in Samsung Electronics,” in ", False),
                          ("Proc. IEEE Conf. Software Testing, Verification and Validation "
                           "(ICST)", True), (", 2023, pp. 317–326.", False)],
    "boboila2010write": [("S. Boboila and P. Desnoyers, “Write endurance in flash drives: "
                          "Measurements and analysis,” in ", False),
                         ("Proc. 8th USENIX Conf. File and Storage Technologies (FAST)", True),
                         (", 2010.", False)],
    "templeman2012gangrene": [("R. Templeman and A. Kapadia, “GANGRENE: Exploring the "
                               "mortality of flash memory,” in ", False),
                              ("Proc. 7th USENIX Workshop on Hot Topics in Security (HotSec)",
                               True), (", 2012.", False)],
    "lui2017experiment": [("G. Lui, “Experiment: 8Gb USB flash drive endurance test,” ", False),
                          ("Gough’s Tech Zone", True), (" (blog), May 2017.", False)],
    "zheng2013understanding": [("M. Zheng, J. Tucek, F. Qin, and M. Lillibridge, "
                                "“Understanding the robustness of SSDs under power fault,” in ",
                                False),
                               ("Proc. 11th USENIX Conf. File and Storage Technologies (FAST)",
                                True), (", 2013, pp. 271–284.", False)],
    "schroeder2016flash": [("B. Schroeder, R. Lagisetty, and A. Merchant, “Flash reliability "
                            "in production: The expected and the unexpected,” in ", False),
                           ("Proc. 14th USENIX Conf. File and Storage Technologies (FAST)",
                            True), (", 2016, pp. 67–80.", False)],
    "meza2015large": [("J. Meza, Q. Wu, S. Kumar, and O. Mutlu, “A large-scale study of flash "
                       "memory failures in the field,” in ", False),
                      ("Proc. ACM SIGMETRICS", True), (", 2015, pp. 177–190.", False)],
    "mielke2006recovery": [("N. Mielke, H. Belgal, A. Fazio, Q. Meng, and N. Righos, “Recovery "
                            "effects in the distributed cycling of flash memories,” in ", False),
                           ("Proc. IEEE Int. Reliability Physics Symp.", True),
                           (", 2006, pp. 29–35.", False)],
    "tseng2011understanding": [("H.-W. Tseng, L. Grupp, and S. Swanson, “Understanding the "
                                "impact of power loss on flash memory,” in ", False),
                               ("Proc. 48th Design Automation Conf. (DAC)", True),
                               (", 2011, pp. 35–40.", False)],
    "guo2017ffd": [("Z. Guo, X. Xu, M. M. Tehranipoor, and D. Forte, “FFD: A framework for "
                    "fake flash detection,” in ", False),
                   ("Proc. 54th Design Automation Conf. (DAC)", True),
                   (", 2017, pp. 1–6.", False)],
    "bogeholz2008h2testw": [("H. Bögeholz, “H2testw,” version 1.4, c’t magazin, heise, "
                             "2008.", False)],
    "machado2026f3": [("M. Machado, “f3: Fight Flash Fraud,” version 10.0, open-source "
                       "software, 2026.", False)],
    "kaplan1958nonparametric": [("E. L. Kaplan and P. Meier, “Nonparametric estimation from "
                                 "incomplete observations,” ", False),
                                ("J. Amer. Statist. Assoc.", True),
                                (", vol. 53, no. 282, pp. 457–481, 1958.", False)],
    "elsayed2021reliability": [("E. A. Elsayed, ", False), ("Reliability Engineering", True),
                               (", 3rd ed. Hoboken, NJ, USA: Wiley, 2021.", False)],
    "meeker2022statistical": [("W. Q. Meeker, L. A. Escobar, and F. G. Pascual, ", False),
                              ("Statistical Methods for Reliability Data", True),
                              (", 2nd ed. Hoboken, NJ, USA: Wiley, 2022.", False)],
    "lu1993using": [("C. J. Lu and W. Q. Meeker, “Using degradation measures to estimate a "
                     "time-to-failure distribution,” ", False), ("Technometrics", True),
                    (", vol. 35, no. 2, pp. 161–174, 1993.", False)],
    "mielke2008bit": [("N. Mielke et al., “Bit error rate in NAND flash memories,” in ", False),
                      ("Proc. IEEE Int. Reliability Physics Symp.", True),
                      (", 2008, pp. 9–19.", False)],
    "gal2005algorithms": [("E. Gal and S. Toledo, “Algorithms and data structures for flash "
                           "memories,” ", False), ("ACM Comput. Surv.", True),
                          (", vol. 37, no. 2, pp. 138–163, 2005.", False)],
    "chung2009survey": [("T.-S. Chung, D.-J. Park, S. Park, D.-H. Lee, S.-W. Lee, and H.-J. Song, "
                         "“A survey of flash translation layer,” ", False),
                        ("J. Syst. Archit.", True),
                        (", vol. 55, no. 5–6, pp. 332–343, 2009.", False)],
    "mylavarapu2009fsaf": [("S. K. Mylavarapu, S. Choudhuri, A. Shrivastava, J. Lee, and "
                            "T. Givargis, “FSAF: File system aware flash translation layer for "
                            "NAND flash memories,” in ", False), ("Proc. DATE", True),
                           (", 2009, pp. 399–404.", False)],
    "jedec2018jesd22a117": [("JEDEC, “Electrically erasable programmable ROM (EEPROM) "
                             "program/erase endurance and data retention test,” JEDEC Standard "
                             "JESD22-A117E, 2018.", False)],
    "jedec2026jesd47": [("JEDEC, “Stress test driven qualification of integrated circuits,” "
                         "JEDEC Standard JESD47N, 2026.", False)],
    "jedec2016jep122": [("JEDEC, “Failure mechanisms and models for semiconductor devices,” "
                         "JEDEC Publication JEP122H, 2016.", False)],
    "mielke2017reliability": [("N. R. Mielke, R. E. Frickey, I. Kalastirsky, M. Quan, "
                               "D. Ustinov, and V. J. Vasudevan, “Reliability of solid-state "
                               "drives based on NAND flash memory,” ", False),
                              ("Proc. IEEE", True),
                              (", vol. 105, no. 9, pp. 1725–1750, 2017.", False)],
    "jurenka2010exploring": [("M. G. Jurenka, “Exploring managed NAND media endurance,” M.S. "
                              "thesis, Boise State Univ., Boise, ID, USA, 2010.", False)],
    "bjorling2010performing": [("M. Bjørling, L. Le Folgoc, A. Mseddi, P. Bonnet, L. Bouganim, "
                                "and B. Jónsson, “Performing sound flash device measurements: "
                                "Some lessons from uFLIP,” in ", False),
                               ("Proc. ACM SIGMOD", True), (", 2010, pp. 1219–1222.", False)],
    "bouganim2009uflip": [("L. Bouganim, B. T. Jónsson, and P. Bonnet, “uFLIP: Understanding "
                           "flash IO patterns,” in ", False),
                          ("Proc. 4th Biennial Conf. Innovative Data Systems Research (CIDR)",
                           True), (", 2009.", False)],
    "maneas2020study": [("S. Maneas, K. Mahdaviani, T. Emami, and B. Schroeder, “A study of SSD "
                         "reliability in large scale enterprise storage deployments,” in ", False),
                        ("Proc. 18th USENIX Conf. File and Storage Technologies (FAST)", True),
                        (", 2020, pp. 137–149.", False)],
    "narayanan2016ssd": [("I. Narayanan et al., “SSD failures in datacenters: What? When? and "
                          "Why?,” in ", False), ("Proc. 9th ACM Int. Systems and Storage Conf.",
                                                 True), (", 2016, pp. 1–11.", False)],
    "zambelli2020mitigating": [("C. Zambelli, L. Zuolo, L. Crippa, R. Micheloni, and P. Olivo, "
                                "“Mitigating self-heating in solid state drives for industrial "
                                "internet-of-things edge gateways,” ", False),
                               ("Electronics", True), (", vol. 9, no. 7, p. 1179, 2020.", False)],
    "iec2008weibull": [("IEC, “Weibull analysis,” International Standard IEC 61649:2008, "
                        "ed. 2.0, 2008.", False)],
    "nelson1972theory": [("W. Nelson, “Theory and applications of hazard plotting for censored "
                          "failure data,” ", False), ("Technometrics", True),
                         (", vol. 14, no. 4, pp. 945–966, 1972.", False)],
    "nelson1995confidence": [("W. Nelson, “Confidence limits for recurrence data, applied to "
                              "cost or number of product repairs,” ", False),
                             ("Technometrics", True),
                             (", vol. 37, no. 2, pp. 147–157, 1995.", False)],
    "meeker1998accelerated": [("W. Q. Meeker, L. A. Escobar, and C. J. Lu, “Accelerated "
                               "degradation tests: Modeling and analysis,” ", False),
                              ("Technometrics", True),
                              (", vol. 40, no. 2, pp. 89–99, 1998.", False)],
    "zhao2004accelerated": [("W. Zhao and E. A. Elsayed, “An accelerated life testing model "
                             "involving performance degradation,” in ", False),
                            ("Proc. Annu. Reliability and Maintainability Symp. (RAMS)", True),
                            (", 2004, pp. 324–329.", False)],
    "elsayed2012overview": [("E. A. Elsayed, “Overview of reliability testing,” ", False),
                            ("IEEE Trans. Rel.", True),
                            (", vol. 61, no. 2, pp. 282–291, 2012.", False)],
    "nelson1990accelerated": [("W. Nelson, ", False),
                              ("Accelerated Testing: Statistical Models, Test Plans, and Data "
                               "Analyses", True), (". New York, NY, USA: Wiley, 1990.", False)],
}

#: Inline markup in slide text: %x% italic, _{x} subscript, ^{x} superscript, {{x}} code.
MARKUP = re.compile(r"(%[^%]+%|_\{[^}]*\}|\^\{[^}]*\}|\{\{[^}]*\}\})")
CITE = re.compile(r"\s*\[\d+(?:\]?[–,]\s*\[?\d+)*\]")


def runs_of(text: str, opts: dict) -> list[tuple[str, dict]]:
    out = []
    for part in MARKUP.split(text):
        if not part:
            continue
        o = dict(opts)
        if part.startswith("%") and part.endswith("%") and len(part) > 1:
            part, o["italic"] = part[1:-1], True
        elif part.startswith("_{"):
            part, o["baseline"] = part[2:-1], -25000
        elif part.startswith("^{"):
            part, o["baseline"] = part[2:-1], 30000
        elif part.startswith("{{"):
            part, o["font"] = part[2:-2], MONO
        out.append((part, o))
    return out


def plain(text: str) -> str:
    """Slide text as the speaker reads it: no markup, no citation marks."""
    text = CITE.sub("", text)
    text = re.sub(r"\{\{([^}]*)\}\}", r"\1", text)
    text = re.sub(r"[_^]\{([^}]*)\}", r"\1", text)
    return text.replace("%", "").replace("  ", " ").strip()


def tb(slide, x, y, w, h, paragraphs, *, name, align=PP_ALIGN.LEFT, spacing=1.12,
       space_after=0, anchor=MSO_ANCHOR.TOP):
    """A text box with zero insets; each paragraph is a list of (text, options) runs.

    Options: size, bold, italic, color, font, baseline (sub- or superscript); the text of
    a run may carry the inline markup of ``runs_of``.
    """
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    shape.name = name
    tf = shape.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_top = tf.margin_bottom = 0
    tf.margin_right = Inches(0.06)  # room for the space at the end of a wrapped line
    tf.vertical_anchor = anchor
    for i, para in enumerate(paragraphs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        p.space_after = Pt(space_after)
        for text, opts in para:
            for part, o in runs_of(text, opts):
                r = p.add_run()
                r.text = part
                f = r.font
                f.size = Pt(o.get("size", TEXT))
                f.bold = o.get("bold", False)
                f.italic = o.get("italic", False)
                f.color.rgb = o.get("color", BODY)
                f.name = o.get("font", "Arial")
                if "baseline" in o:
                    r.font._element.set("baseline", str(o["baseline"]))
    return shape


def lines_of(text: str, w: float, size: float, em: float = 0.58) -> int:
    """Estimated wrapped lines of ``text`` in a box ``w`` inches wide (Arial).

    ``em`` is the mean character width in ems: 0.58 leaves room for bold lead-ins and
    for words that wrap early, 0.5 suits short plain items.
    """
    per_line = max(1, int(w * 72 / (size * em)))
    return max(1, math.ceil(len(plain(text)) / per_line))


def prose(slide, x, y, w, items, *, size=TEXT, gap=9, spacing=1.12, name="prose", to=BOTTOM):
    """Paragraphs with an optional bold lead-in; returns the estimated bottom (inches).

    The box reaches down to ``to`` (the free space below it), or further if the estimate
    needs more, so that the layout check measures the text against the space it has.
    """
    paras, h = [], 0.0
    for item in items:
        head, body = item if isinstance(item, tuple) else ("", item)
        runs = []
        if head:
            runs.append((head + " ", {"size": size, "bold": True, "color": NAVY}))
        runs.append((body, {"size": size}))
        paras.append(runs)
        h += lines_of(f"{head} {body}", w, size) * size * spacing / 72 + gap / 72
    tb(slide, x, y, w, max(h, to - y), paras, name=name, spacing=spacing, space_after=gap)
    return y + h


def numbered(slide, x, y, w, items, *, size=SMALL, row=0.5, name="list", color=CRIMSON,
             rules=True):
    """A numbered list with the numbers in the accent and hairlines between the items."""
    for i, text in enumerate(items):
        n = lines_of(text, w - 0.45, size, em=0.54)
        h = max(row, n * size * 1.2 / 72 + 0.16)
        tb(slide, x, y + 0.06, 0.4, 0.35, [[(str(i + 1), {"size": size + 2, "bold": True,
                                                         "color": color})]],
           name=f"{name} n{i}", spacing=1.0)
        tb(slide, x + 0.45, y + 0.07, w - 0.45, h - 0.1, [[(text, {"size": size})]],
           name=f"{name} t{i}", spacing=1.1)
        y += h
        if rules and i < len(items) - 1:
            hairline(slide, x + 0.45, y - 0.03, w - 0.45, f"{name} rule {i}")
    return y


def card(slide, x, y, w, h, head, body, *, name, number=None, sub=None, size=SMALL):
    box(slide, x, y, w, h, name)
    runs = []
    if number:
        runs.append((f"{number}  ", {"size": NOTE + 1, "bold": True, "color": CRIMSON}))
    runs.append((head, {"size": TEXT + 1, "bold": True, "color": NAVY}))
    paras = [runs]
    if sub:
        paras.append([(sub, {"size": NOTE, "color": MUTED})])
    tb(slide, x + 0.22, y + 0.17, w - 0.42, 0.7, paras, name=f"{name} head", spacing=1.1)
    top = y + 0.17 + (0.62 if sub else 0.4)
    tb(slide, x + 0.22, top, w - 0.42, h - (top - y) - 0.12, [[(body, {"size": size})]],
       name=f"{name} body", spacing=1.12)


def image_size(path: Path) -> tuple[float, float]:
    with Image.open(path) as im:
        return im.size


def place(slide, path: Path, x, y, *, w=None, h=None, name, alt=""):
    """A picture at a given width or height (the other side from its aspect ratio)."""
    pw, ph = image_size(path)
    if w is None:
        w = h * pw / ph
    return picture(slide, path, x, y, w=w, name=name, alt=alt)


# -- figures ------------------------------------------------------------------------------
EQUATIONS = {
    "eq_cycle": r"t_{\mathrm{c}} = \frac{V}{w} + \frac{V}{r} + t_0",
}


def draw_figures(out: Path) -> None:
    """The plan's figures at slide size, and the equations as pictures."""
    import make_plan_figures as plan
    from flashrel.viz import style

    out.mkdir(parents=True, exist_ok=True)
    style.apply()
    plan.FORMATS = ("png",)
    plan.fig_rig(out)
    plan.fig_cycle(out)
    drives, hours = plan.planning_inputs()
    for name, draw in plan.PANELS.items():
        fig, ax = plt.subplots(figsize=(2.9, 2.05))
        draw(ax, drives, hours)
        style.save(fig, out / f"panel_{name}", ("png",), width=2.9)
    with mpl.rc_context({"mathtext.fontset": "stix", "font.family": "STIXGeneral"}):
        for name, tex in EQUATIONS.items():
            fig = plt.figure(figsize=(0.2, 0.2))
            fig.text(0, 0, f"${tex}$", fontsize=21, color="#1B2A41")
            fig.savefig(out / f"{name}.png", dpi=EQ_DPI, bbox_inches="tight", pad_inches=0.03,
                        transparent=True)
            plt.close(fig)


# -- the deck -----------------------------------------------------------------------------
class ProposalDeck:
    def __init__(self, template: Path, figures: Path, qr: Path | None):
        self.prs = Presentation(str(template))
        self.figs = figures
        self.qr = qr
        self.refs: list[str] = []
        self.layout = next(l for l in self.prs.slide_layouts if l.name == "CONTENT")
        slides = list(self.prs.slides)
        self.number_sp = copy.deepcopy(next(
            sh for sh in slides[1].shapes if sh.is_placeholder
            and sh.placeholder_format.type == PP_PLACEHOLDER.SLIDE_NUMBER)._element)
        photos = {sh.name: sh.image.blob for sh in slides[3].shapes if sh.shape_type == 13}
        self.photo_sandisk, self.photo_ablaze = photos["drives sandisk"], photos["drives ablaze"]
        self.cover, self.closing = slides[0], slides[-1]
        for s in slides[1:-1]:  # the first deck's content slides are rebuilt, not reused
            Presentation10.drop(self, s)
        for k, s in enumerate((self.cover, self.closing)):
            s.part.partname = PackURI(f"/ppt/slides/slide{900 + k}.xml")

    # -- structure ----------------------------------------------------------------------
    def cite(self, *keys: str) -> str:
        """IEEE citation marks, numbered in the order of first use, ranges for runs of 3+."""
        nums = []
        for k in keys:
            if k not in REFS:
                raise KeyError(k)
            if k not in self.refs:
                self.refs.append(k)
            nums.append(self.refs.index(k) + 1)
        nums = sorted(set(nums))
        groups, start = [], nums[0]
        for a, b in zip(nums, nums[1:] + [None]):
            if b != a + 1:
                groups.append((start, a))
                start = b
        parts = [f"[{a}]–[{b}]" if b - a >= 2 else ", ".join(f"[{n}]" for n in range(a, b + 1))
                 for a, b in groups]
        return ", ".join(parts)

    def slide(self, section: str | None, title: str, lead: str | None):
        s = self.prs.slides.add_slide(self.layout)
        sp = copy.deepcopy(self.number_sp)
        s.shapes._spTree.append(sp)
        sp.nvSpPr.cNvPr.id = max(int(e.get("id")) for e in s.shapes._spTree.iter()
                                 if e.tag.endswith("}cNvPr") and e.get("id")) + 1
        for ph in list(s.placeholders):
            kind = ph.placeholder_format.type
            tf = ph.text_frame
            tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
            if kind == PP_PLACEHOLDER.TITLE:
                ph.left, ph.top, ph.width, ph.height = (Inches(X0), Inches(0.80), Inches(W),
                                                        Inches(0.62))
                tf.text = title
                tf.paragraphs[0].runs[0].font.size = Pt(TITLE)
            elif kind == PP_PLACEHOLDER.BODY:
                if section in SECTIONS:
                    ph.left, ph.top, ph.width, ph.height = (Inches(X0), Inches(0.48),
                                                            Inches(W), Inches(0.28))
                    self.tracker(tf.paragraphs[0], section)
                else:
                    ph._element.getparent().remove(ph._element)
        Presentation10.fit_number(sp)
        if lead:
            tb(s, X0, 1.50, W, 0.62, [[(lead, {"size": 18})]], name="lead", spacing=1.08)
        return s

    @staticmethod
    def tracker(p, section: str):
        for k, name in enumerate(SECTIONS):
            if k:
                sep = p.add_run()
                sep.text = "   ·   "
                sep.font.size, sep.font.bold, sep.font.color.rgb = Pt(11), False, MUTED
            r = p.add_run()
            r.text = name.upper()
            r.font.size, r.font.bold = Pt(11), name == section
            r.font.color.rgb = CRIMSON if name == section else MUTED
            r.font._element.set("spc", "100")

    @staticmethod
    def notes(s, *texts: str):
        s.notes_slide.notes_text_frame.text = " ".join(plain(t) for t in texts if t)

    def restyle(self):
        theme = self.prs.slide_master.part.part_related_by(RT.THEME)
        xml = theme.blob.decode("utf-8")
        for old, new in RECOLOR.items():
            xml = xml.replace(f'<a:srgbClr val="{old}"/>', f'<a:srgbClr val="{new}"/>')
        theme._blob = xml.encode("utf-8")

    # -- cover and closing ----------------------------------------------------------------
    def cover_slide(self):
        s = self.cover
        recolor_xml(s._element)
        Presentation10.navy_panel(self, s, "cover panel")
        for sh in s.shapes:
            if not sh.has_text_frame:
                continue
            title = sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.TITLE
            if title:
                sh.height = Inches(1.75)
            for i, p in enumerate(sh.text_frame.paragraphs):
                for r in p.runs:
                    r.font.color.rgb = (WHITE if title else ACCENT_LIGHT if sh.name == "label"
                                        else LIGHT_MUTED if sh.name == "course" or i > 0
                                        else WHITE)
        tb(s, X0, 4.2, 6.3, 0.4, [[("Test plan  ·  eight drives  ·  24 hours  ·  "
                                    "October 28 to 29, 2026", {"size": 16, "color": LIGHT})]],
           name="teaser", spacing=1.0)
        self.notes(s, "We are Guoan Wang, Shangqing Wei, and Mengtong Ai. This is our test plan "
                      "for Project 4, a life test of USB flash drives under repeated copy, "
                      "verify, and delete cycling. We follow the order of the plan.")

    def closing_slide(self):
        s = self.closing
        recolor_xml(s._element)
        Presentation10.navy_panel(self, s, "closing panel")
        for sh in list(s.shapes):
            if sh.is_placeholder and sh.has_text_frame and not sh.text_frame.text.strip():
                sh._element.getparent().remove(sh._element)
                continue
            if not sh.has_text_frame:
                continue
            title = sh.is_placeholder and sh.placeholder_format.type == PP_PLACEHOLDER.TITLE
            if title:
                sh.height = Inches(0.8)
            for i, p in enumerate(sh.text_frame.paragraphs):
                for r in p.runs:
                    r.font.color.rgb = WHITE if title or i == 0 else LIGHT_MUTED
        if self.qr and self.qr.exists():
            box(s, X0, 4.55, 1.5, 1.5, "qr panel", fill=WHITE, radius=0.06)
            picture(s, self.qr, X0 + 0.1, 4.65, w=1.3, name="qr code",
                    alt="QR code of the project repository on GitHub")
            tb(s, X0 + 1.8, 4.95, 4.6, 0.8, [
                [("Test software", {"size": SMALL, "bold": True, "color": WHITE})],
                [("scan to open the GitHub repository", {"size": NOTE, "color": LIGHT_MUTED})]],
               name="qr caption", spacing=1.2)
        self.notes(s, "Thank you. The test software is in the project repository. We are "
                      "happy to take questions and feedback on the plan.")

    # -- introduction ---------------------------------------------------------------------
    def intro(self):
        lead = "USB flash drives wear out, and no rating or large test tells a buyer how soon."
        s = self.slide("Background", "Motivation and Research Questions", lead)
        items = [("Wear-out.", "Each NAND cell survives a limited number of program and erase "
                  "cycles, and the controller hides the rising error rate until it can no "
                  f"longer correct it {self.cite('cai2017error')}."),
                 ("Symptoms.", "Users then see files that read back wrong, input/output errors, "
                  "drives that drop off the bus, and drives that turn read-only "
                  f"{self.cite('boboila2010write', 'templeman2012gangrene', 'lui2017experiment')}."),
                 ("No rating.", "SSDs carry an endurance rating under JESD218 "
                  f"{self.cite('jedec2026jesd218')}, and USB drives have none. A buyer cannot "
                  "compare a name-brand drive with a low-cost one.")]
        prose(s, X0, TOP, 5.5, items)
        x, w = 6.85, RIGHT - 6.85
        heading(s, x, TOP, w, "Research questions",
                "response: cycles to hard failure, T, of each drive", name="rq")
        rqs = [("Manufacturer", "At 8 GB, do SanDisk and ABLAZE drives differ in cycles to "
                                "hard failure?"),
               ("Capacity", "Within ABLAZE, do 16 GB drives fail after a different number of "
                            "cycles than 8 GB drives?"),
               ("File size", "Does the file size change the chance that a cycle fails and "
                             "the rate of slowdown?"),
               ("Warning signs", "Which indicators come before a hard failure, and by how "
                                 "many cycles?")]
        for i, (head, body) in enumerate(rqs):
            y = TOP + 0.78 + i * 0.95
            tb(s, x, y - 0.02, 0.45, 0.5, [[(str(i + 1), {"size": 22, "bold": True,
                                                         "color": CRIMSON})]],
               name=f"rq n{i}", spacing=1.0)
            tb(s, x + 0.55, y, w - 0.55, 0.3, [[(head, {"size": TEXT, "bold": True,
                                                        "color": NAVY})]], name=f"rq h{i}")
            tb(s, x + 0.55, y + 0.31, w - 0.55, 0.52, [[(body, {"size": SMALL})]],
               name=f"rq t{i}", spacing=1.05)
            if i < len(rqs) - 1:
                hairline(s, x + 0.55, y + 0.88, w - 0.55, f"rq rule {i}")
        self.notes(s, lead, *[f"{h} {b}" for h, b in items],
                   "We ask four questions, about manufacturer, capacity, file size, and warning "
                   "signs. The response is the number of cycles to hard failure of each drive.")

    # -- literature review ----------------------------------------------------------------
    def wear(self):
        lead = "Every program and erase cycle damages a cell, and denser flash wears out sooner."
        s = self.slide("Background", "How NAND Flash Wears Out", lead)
        items = [("Damage.", "Programming and erasing a cell pushes charge through a thin "
                  "oxide, and the damage builds up with every program/erase (P/E) cycle."),
                 ("Error sources.", "Raw bit errors come from wear, programming errors, "
                  "interference from neighboring cells, charge loss over time, and read "
                  f"disturb {self.cite('mielke2008bit', 'cai2017error')}."),
                 ("Cell density.", "Older SSDs with one bit per cell withstood about 150,000 "
                  "P/E cycles, and 1x-nm flash with two bits per cell about 3,000 "
                  f"{self.cite('cai2017error')}. Each extra bit per cell cuts the rated life by "
                  f"a factor of 10 to 20 {self.cite('grupp2012bleak')}."),
                 ("Our drives.", "Low-cost USB drives use dense flash, so their endurance is "
                  "likely at the low end of this range.")]
        prose(s, X0, TOP, 6.3, items)
        x, w = 7.55, RIGHT - 7.55
        heading(s, x, TOP, w, "Rated program/erase cycles", name="tiles")
        tiles = [("150,000", "P/E cycles of older flash with one bit per cell "
                  + self.cite("cai2017error")),
                 ("3,000", "P/E cycles of 1x-nm flash with two bits per cell "
                  + self.cite("cai2017error")),
                 ("10–20×", "shorter rated life for each extra bit per cell "
                  + self.cite("grupp2012bleak"))]
        y = TOP + 0.5
        for i, (value, label) in enumerate(tiles):
            box(s, x, y, w, 1.1, f"tile {i}")
            tb(s, x + 0.25, y, 1.9, 1.1, [[(value, {"size": 28, "bold": True,
                                                   "color": NAVY})]],
               name=f"tile {i} value", anchor=MSO_ANCHOR.MIDDLE, spacing=1.0)
            tb(s, x + 2.2, y, w - 2.4, 1.1, [[(label, {"size": SMALL})]],
               name=f"tile {i} label", anchor=MSO_ANCHOR.MIDDLE, spacing=1.1)
            y += 1.25
        self.notes(s, lead, *[f"{h} {b}" for h, b in items])

    def ftl(self):
        lead = "The controller stands between the host and the cells, so a host cycle is not a P/E cycle."
        s = self.slide("Background", "Flash Translation Layer and Recovery", lead)
        items = [("Out-of-place writes.", "A flash translation layer writes data out of place, "
                  "collects garbage, and spreads wear over all blocks "
                  f"{self.cite('gal2005algorithms', 'chung2009survey')}."),
                 ("Write amplification.", "Small random writes raise the number of flash page "
                  f"writes per host page write {self.cite('hu2009write')}."),
                 ("Deleted files.", "FAT-type file systems do not tell the controller which data "
                  "were deleted, so deleted files still look valid inside the drive "
                  f"{self.cite('mylavarapu2009fsaf')}."),
                 ("Recovery.", "Damage partly recovers in the pauses between cycles, which can "
                  "lengthen the retention life after cycling by more than ten times "
                  f"{self.cite('mielke2006recovery')}.")]
        prose(s, X0, TOP, 6.6, items, size=SMALL + 0.5, gap=8)
        x, w = 7.85, RIGHT - 7.85
        box(s, x, TOP, w, 3.3, "consequences")
        tb(s, x + 0.3, TOP + 0.22, w - 0.6, 0.35, [[("What this means for the test",
                                                      {"size": SMALL, "bold": True,
                                                       "color": NAVY})]],
           name="consequences head", spacing=1.0)
        cons = [f"A host cycle is not a NAND P/E cycle {self.cite('meza2015large')}, so we "
                "report life both in host cycles and in data written.",
                "Back-to-back cycling is harsher than use in the field."]
        numbered(s, x + 0.3, TOP + 0.65, w - 0.6, cons, size=TEXT, row=0.5, name="cons")
        self.notes(s, lead, *[f"{h} {b}" for h, b in items], *cons)

    def standards(self):
        lead = "JEDEC standards rate the endurance of SSDs, and the workload changes the result."
        s = self.slide("Background", "How Industry Tests Endurance", lead)
        items = [("JESD218.", "The rating is the most terabytes that a host can write while the "
                  "drive keeps its capacity, its error limits, and its data with power off "
                  f"{self.cite('jedec2026jesd218')}."),
                 ("Client limits.", "A client drive may have at most 3 % functional failures "
                  "and an unrecoverable bit error ratio of at most 10^{−15}. A test that would "
                  "need more than 1,000 hours may extrapolate with lognormal or Weibull plots "
                  f"{self.cite('cox2010jedec')}."),
                 ("Workloads.", "JESD219 fills its workloads with random data, so compression "
                  "in the drive gives no advantage "
                  f"{self.cite('jedec2022jesd219', 'cox2010jedec')}."),
                 ("Other standards.", "JESD22-A117 tests single memory components, JESD47 and "
                  "JEP122 give the qualification framework and failure models "
                  f"{self.cite('jedec2018jesd22a117', 'jedec2026jesd47', 'jedec2016jep122')}, "
                  f"and Mielke et al. review their use {self.cite('mielke2017reliability')}.")]
        prose(s, X0, TOP, 6.5, items, size=SMALL + 0.5, gap=8)
        x, w = 7.7, RIGHT - 7.7
        box(s, x, TOP, w, BOTTOM - TOP, "kim card")
        kim = self.cite("kim2023automating")
        tb(s, x + 0.3, TOP + 0.22, w - 0.6, 0.35, [[(f"A workload study at Samsung {kim}",
                                                      {"size": SMALL, "bold": True,
                                                       "color": NAVY})]],
           name="kim head", spacing=1.0)
        study = ["Generated write patterns ran on a 32 GB USB drive and a 256 GB microSD card "
                 "over USB 3.0, with no file system.",
                 "The generated pattern used up the rated life of the USB drive with about "
                 "26 % less data than the pattern of the engineers.",
                 "Workload-free patterns needed 2.5 to 3.4 times less data than workload-based "
                 "ones."]
        prose(s, x + 0.3, TOP + 0.65, w - 0.6, study, size=SMALL, gap=8, to=5.55)
        tb(s, x + 0.3, 5.65, w - 0.6, 0.85, [[("A test must fix its workload and report it "
                                               "exactly.", {"size": 16, "italic": True,
                                                            "font": "Georgia", "color": NAVY})]],
           name="kim lesson", spacing=1.05)
        self.notes(s, lead, *[f"{h} {b}" for h, b in items],
                   "At Samsung, the generated pattern used up the rated life of a USB drive with "
                   "about 26 % less data than the pattern of the engineers. A test must "
                   "therefore fix its workload and report it exactly.")

    def usb_tests(self):
        lead = "Three earlier tests wore out USB drives, each with two to five failures."
        s = self.slide("Background", "Wear-Out Tests of USB Drives", lead)
        cards = [("Boboila and Desnoyers", f"FAST 2010 {self.cite('boboila2010write')}",
                  "Four small drives were written at fixed addresses, and two wore out. Some "
                  "writes stored nothing, the program sometimes exited silently, and other "
                  "writes failed or hung. One drive slowed to about 40 ms per write in its last "
                  "25,000 of 107 million writes."),
                 ("GANGRENE", f"HotSec 2012 {self.cite('templeman2012gangrene')}",
                  "Five 2 GB drives from one lot ran on Windows 7, with random files written "
                  "and deleted. All five turned permanently read-only after 204.6 to 627.1 GB, "
                  "about 100 to 315 times their capacity, and none slowed down before."),
                 ("Lui", f"blog, 2017 {self.cite('lui2017experiment')}",
                  "Three 8 GB drives of different makes rewrote one drive-filling file on exFAT, "
                  "with no read-back while cycling. A SanDisk failed after 632 cycles, a Comsol "
                  "at 743 and 959, and a Verbatim after 9,751.")]
        cw, gap = (W - 0.5) / 3, 0.25
        for i, (head, sub, body) in enumerate(cards):
            card(s, X0 + i * (cw + gap), TOP, cw, 3.65, head, body, name=f"study {i}",
                 sub=sub, size=SMALL)
        note = "None of these studies fitted a life distribution to the cycles to failure."
        tb(s, X0, 6.2, W, 0.4, [[(note, {"size": 16, "italic": True, "font": "Georgia",
                                          "color": NAVY})]], name="usb note", spacing=1.0)
        self.notes(s, lead, *[f"{h}. {b}" for h, _, b in cards], note)

    def conditions(self):
        lead = "Earlier work also shows how random writes, fake drives, and firmware can mislead a test."
        s = self.slide("Background", "Workload, Counterfeits, and Test Conditions", lead)
        items = [("Random writes.", "In a hypothetical usage model of a camera, making half of "
                  "the transfers random-address halved the advertised 10-year life of the card "
                  f"{self.cite('jurenka2010exploring')}."),
                 ("Fake drives.", "Fake flash drives are common, and their chips may be recycled "
                  f"or remarked {self.cite('guo2017ffd')}. Tools such as H2testw and f3 screen a "
                  "fake capacity by filling the drive and reading it back "
                  f"{self.cite('bogeholz2008h2testw', 'machado2026f3')}."),
                 ("Firmware.", "Undocumented firmware governs flash devices, so the device state "
                  "and the test conditions must be controlled "
                  f"{self.cite('bjorling2010performing', 'bouganim2009uflip')}. We fix and "
                  "report both.")]
        prose(s, X0, TOP, 6.6, items)
        x, w = 7.85, RIGHT - 7.85
        box(s, x, TOP, w, 3.3, "plan card")
        tb(s, x + 0.3, TOP + 0.22, w - 0.6, 0.35, [[("In this plan", {"size": SMALL,
                                                                      "bold": True,
                                                                      "color": NAVY})]],
           name="plan card head", spacing=1.0)
        steps = ["One fixed rotation of three file sizes.",
                 "The first cycle reads back 90 % of the capacity.",
                 "The same file system and host for every drive."]
        numbered(s, x + 0.3, TOP + 0.65, w - 0.6, steps, size=TEXT, row=0.5, name="plan")
        self.notes(s, lead, *[f"{h} {b}" for h, b in items],
                   "The box on the right lists what the plan does about each point.")

    def lessons(self):
        lead = "Table I lists what each earlier test did and what our test takes from it."
        s = self.slide("Background", "What Prior Tests Did and What This Plan Adopts", lead)
        rows = [["Source", "Object and setup", "Failure indicators reported",
                 "Adopted in this plan"],
                [f"Boboila and Desnoyers {self.cite('boboila2010write')}",
                 "NAND chips cycled to error, four small USB drives",
                 "silent write failure, I/O error, hung write, one drive slowed first",
                 "byte-exact read-back, per-file latency, hang watchdog (F3)"],
                [f"GANGRENE {self.cite('templeman2012gangrene')}",
                 "five 2 GB drives from one lot, five more as baseline",
                 "read-only lock after 204.6 to 627.1 GB, no warning",
                 "read-only detection (F4), random data, lot recorded"],
                [f"Lui, blog {self.cite('lui2017experiment')}",
                 "three 8 GB drives, exFAT, one file rewritten",
                 "failures at 632, 959, and 9,751 cycles, no read-back",
                 "exFAT on all drives, read-back, disconnects logged (F3)"],
                [f"Kim et al. {self.cite('kim2023automating')}",
                 "32 GB USB drive and microSD, raw writes over USB 3.0",
                 "data written until the rated life is used",
                 "one fixed, documented workload, 90 % fill"],
                [f"Zheng et al. {self.cite('zheng2013understanding')}",
                 "15 SSDs from five vendors, power-fault rig",
                 "bit corruption, shorn writes, metadata corruption",
                 "a header in every 1 MiB chunk that classifies corruption"],
                [f"JESD218 {self.cite('jedec2026jesd218', 'cox2010jedec')}",
                 "SSD endurance rating", "functional failure, UBER, retention, capacity loss",
                 "endurance as data written, capacity checked (F6)"],
                [f"Field studies {self.cite('schroeder2016flash', 'meza2015large')}",
                 "production fleets", "errors grow with wear, heat raises failure rates",
                 "transient errors counted (D3), drives spaced apart"]]
        rows = [rows[0]] + [[[(r[0], {"bold": True, "color": NAVY})], *r[1:]] for r in rows[1:]]
        table(s, X0, TOP - 0.05, [2.4, 3.05, 3.15, W - 8.6], rows, "lessons table",
              head_h=0.36, row_h=0.56, size=12)
        self.notes(s, lead, "From each source we take one or two design choices: byte-exact "
                   "read-back and a hang watchdog from Boboila and Desnoyers, read-only "
                   "detection from GANGRENE, exFAT and read-back from the blog test, a fixed "
                   "workload from Kim et al., chunk headers from Zheng et al., endurance as data "
                   "written from JESD218, and the counting of transient errors from the field "
                   "studies.")

    def field(self):
        lead = "Field data show that errors grow with wear and that failures depend on age and heat."
        s = self.slide("Background", "Evidence from the Field", lead)
        left = [("Google.", "Over six years, raw bit error rates grew close to linearly with "
                 "P/E cycles and exceeded the projections of accelerated life tests "
                 f"{self.cite('schroeder2016flash')}."),
                ("Facebook.", "As wear grew, the failure rate passed through periods of early "
                 "detection, early failure, usable life, and wear-out, and higher temperatures "
                 f"raised it unless the drives throttled {self.cite('meza2015large')}."),
                ("NetApp.", "Failure rates rose for 12 to 15 months, with no bathtub shape "
                 f"{self.cite('maneas2020study')}.")]
        right = [("Datacenters.", "Studies relate failures to symptoms logged before them "
                  f"{self.cite('narayanan2016ssd')}."),
                 ("Power faults.", "Self-describing records with a checksum and the intended "
                  f"address tell corruption types apart {self.cite('zheng2013understanding')}, "
                  "and an interrupted program can damage data written earlier "
                  f"{self.cite('tseng2011understanding')}."),
                 ("Heat.", "Self-heating can throttle a busy SSD, so a drop in throughput can "
                  f"also come from heat {self.cite('zambelli2020mitigating')}.")]
        w = (W - 0.5) / 2
        prose(s, X0, TOP, w, left)
        prose(s, X0 + w + 0.5, TOP, w, right, name="prose right")
        self.notes(s, lead, *[f"{h} {b}" for h, b in left + right])

    def statistics(self):
        lead = "The analysis rests on the methods of the course for censored life data and degradation."
        s = self.slide("Background", "Statistical Methods", lead)
        left = [("Censored life data.", "The course textbook "
                 f"{self.cite('elsayed2021reliability')} and Meeker et al. "
                 f"{self.cite('meeker2022statistical')} cover maximum likelihood with "
                 "right-censored data and probability plotting, and IEC 61649 standardizes "
                 f"Weibull analysis {self.cite('iec2008weibull')}."),
                ("Nonparametric tools.", "The Kaplan–Meier estimator needs no distributional "
                 f"assumption {self.cite('kaplan1958nonparametric')}. Hazard plotting checks a "
                 f"candidate distribution {self.cite('nelson1972theory')}, and the mean "
                 f"cumulative function handles repeated events {self.cite('nelson1995confidence')}.")]
        right = [("Degradation.", "Degradation measurements give life information when few "
                  f"units fail {self.cite('lu1993using', 'meeker1998accelerated')}, and hard "
                  "and soft failures that compete can be modeled jointly "
                  f"{self.cite('zhao2004accelerated')}."),
                 ("Test planning.", "A test can be designed to yield failure data, degradation "
                  "data, or both within a limited duration "
                  f"{self.cite('elsayed2012overview')}. Nelson covers accelerated test models "
                  f"and plans {self.cite('nelson1990accelerated')}.")]
        w = (W - 0.5) / 2
        prose(s, X0, TOP, w, left)
        prose(s, X0 + w + 0.5, TOP, w, right, name="prose right")
        self.notes(s, lead, *[f"{h} {b}" for h, b in left + right])

    def gap(self):
        lead = "No study fits a censored life distribution to USB drives or compares their brands and capacities."
        s = self.slide("Background", "Gap in Earlier Work", lead)
        items = ["No study that we found fits a life distribution, with censoring, to the cycles "
                 "to failure of USB drives, compares brands or capacities with more than one or "
                 "two units each, or varies the file size.",
                 "Our test reads back every byte, tells drive faults apart from port and host "
                 "faults, uses a fixed and documented workload, and analyzes the censored life "
                 "data."]
        prose(s, X0, TOP, 6.6, items)
        x, w = 7.85, RIGHT - 7.85
        box(s, x, TOP, w, 3.1, "scale card")
        tb(s, x + 0.3, TOP + 0.3, w - 0.6, 0.8, [[("100 to 1,000", {"size": 34, "bold": True,
                                                                     "color": NAVY})]],
           name="scale value", spacing=1.0)
        scale = ("full-drive writes at which most low-cost drives failed in these tests "
                 f"{self.cite('templeman2012gangrene', 'lui2017experiment')}. We size the test "
                 "against this range.")
        tb(s, x + 0.3, TOP + 1.15, w - 0.6, 1.7, [[(scale, {"size": TEXT})]],
           name="scale text", spacing=1.12)
        self.notes(s, lead, *items, "Most low-cost drives in earlier tests failed after 100 to "
                   "1,000 full-drive writes, and we size the test against this range.")

    # -- method ---------------------------------------------------------------------------
    def drives(self):
        lead = "Nine drives from three purchase lots: eight are tested, and the ninth is a spare."
        s = self.slide("Method", "Drives Under Test", lead)
        items = [("Three lots.", "We test two SanDisk 8 GB, four ABLAZE 8 GB, and two ABLAZE "
                  "16 GB drives. A fifth ABLAZE 8 GB drive, A8-05, is the spare."),
                 ("Comparisons.", "Brand is compared at 8 GB, S8 against A8, and capacity "
                  "within ABLAZE, A8 against A16."),
                 ("Intake.", "Each drive is formatted to exFAT and gets an identity file, so "
                  "the software never relies on drive letters."),
                 ("Capacity screen.", "The first cycle reads back 90 % of the capacity, as "
                  f"H2testw and f3 do {self.cite('bogeholz2008h2testw', 'machado2026f3')}.")]
        prose(s, X0, TOP, 5.3, items)
        x = 6.4
        widths = [0.84, 1.94, 0.85, 1.3, 0.63]
        widths.append(RIGHT - x - sum(widths))
        pw = sum(widths)
        box(s, x, TOP, pw, 1.6, "photo panel")
        for blob, px, alt in ((self.photo_sandisk, x + 0.55, "SanDisk Cruzer Blade drives"),
                              (self.photo_ablaze, x + 3.2, "ABLAZE metal keychain drives")):
            pic = s.shapes.add_picture(io.BytesIO(blob), Inches(px), Inches(TOP + 0.08),
                                       height=Inches(1.44))
            pic.name = "photo"
            pic._element.nvPicPr.cNvPr.set("descr", alt)
        rows = [["Group", "Drive", "Capacity", "Listed write", "Price", "n"],
                ["S8", "SanDisk Cruzer Blade", "8 GB", "up to 60 MB/s", "$9.99", "2"],
                ["A8", "ABLAZE keychain", "8 GB", "10 (no unit)", "$4.40", "4 + 1"],
                ["A16", "ABLAZE keychain", "16 GB", "10 (no unit)", "$7.50", "2"]]
        y_tab, head_h, row_h = TOP + 1.8, 0.4, 0.44
        rows = [rows[0]] + [[[("      " + r[0], {"bold": True, "color": NAVY})], *r[1:]]
                            for r in rows[1:]]
        table(s, x, y_tab, widths, rows, "drives table", head_h=head_h, row_h=row_h,
              size=13, align="lllrrr")
        for i, g in enumerate(("S8", "A8", "A16")):
            y = y_tab + head_h + (i + 0.5) * row_h - 0.09
            box(s, x, y, 0.18, 0.18, f"swatch {g}", fill=GROUP_COLOR[g],
                shape=MSO_SHAPE.RECTANGLE)
        note = ("SanDisk housings are plastic and ABLAZE housings metal. The listed speeds are "
                "marketing figures, so the pilot measures the real speeds. + 1: the spare "
                "A8-05.")
        tb(s, x, y_tab + head_h + 3 * row_h + 0.12, pw, 0.6,
           [[(note, {"size": NOTE, "italic": True, "color": MUTED})]], name="drives note",
           spacing=1.1)
        self.notes(s, lead, *[f"{h} {b}" for h, b in items])

    def rig(self):
        lead = ("All eight drives cycle at the same time on one Windows desktop, each driven by "
                "its own worker process.")
        s = self.slide("Method", "Test Rig", lead)
        path = self.figs / "fig_rig.png"
        pw, ph = image_size(path)
        h = 4.2
        w = h * pw / ph
        place(s, path, X0 + (W - w) / 2, 2.15, h=h, name="rig figure",
              alt="Test rig: host W, six desktop ports, the USB-C dock, and the data path")
        cap = ("The dock holds one SanDisk and one 16 GB drive, so each comparison has a group "
               "on both connection paths.")
        tb(s, X0, 6.42, W, 0.3, [[(cap, {"size": NOTE + 1, "italic": True, "color": MUTED})]],
           name="rig caption", align=PP_ALIGN.CENTER, spacing=1.0)
        self.notes(s, lead, "Six drives use the USB-A ports of the desktop, W1 to W6, and two "
                   "use a USB-C dock. The spare A8-05 stays unplugged.", cap,
                   "On the right, the logs of each worker are copied to cloud storage and turned "
                   "into the life table, the model fits, and an Excel workbook.")

    def software(self):
        lead = "A supervisor runs one worker per drive, and a fixed list of commands runs the test."
        s = self.slide("Method", "Test Software and Commands", lead)
        items = [("One host.", "A single desktop keeps differences between computers out of "
                  "the comparisons. Workers start 30 s apart."),
                 ("Supervisor.", "It restarts a crashed worker and flags a drive with no "
                  "progress for 10 min as hung."),
                 ("Logs.", "Every record is flushed to disk at once, and seventy automated "
                  "tests with injected faults cover the software.")]
        prose(s, X0, TOP, 6.3, items)
        x, w = 7.5, RIGHT - 7.5
        heading(s, x, TOP, w, "Commands, in the order they are run", name="commands")
        rows = [["Command", "Purpose"],
                ["enroll", "identity file and intake record"],
                ["selftest", "check cache bypass and fault detection"],
                ["run", "cycle all drives until the stop time"],
                ["status", "cycles, data written, last outcome"],
                ["recheck", "post-test check of a failed drive"],
                ["portcheck", "test a port with the spare drive"],
                ["port-fault", "move a failure to the port (F5)"],
                ["retire", "record a hard failure"],
                ["export", "build the Excel workbook"],
                ["analyze", "life table, fits, and figures"]]
        gf = table(s, x, TOP + 0.45, [1.45, w - 1.45], rows, "commands table", head_h=0.36,
                   row_h=0.355, size=13)
        for r in range(1, len(rows)):
            for run in gf.table.cell(r, 0).text_frame.paragraphs[0].runs:
                run.font.name = MONO
                run.font.color.rgb = NAVY
        self.notes(s, lead, *[f"{h} {b}" for h, b in items],
                   "On the right are the commands we run, from enroll to analyze.")

    def workloads(self):
        lead = ("Each cycle fills 90 % of the free space with one file size, and every chunk of "
                "data identifies itself.")
        s = self.slide("Method", "Workloads and Self-Checking Data", lead)
        heading(s, X0, TOP, 6.0, "Workloads (file-size classes)", name="workloads")
        rows = [["Workload", "File sizes", "Per folder", "Files, 8 GB", "Files, 16 GB"],
                ["Large", "512 MiB to 2 GiB", "8", "5", "11"],
                ["Medium", "1 to 64 MiB", "64", "407", "825"],
                ["Small", "4 to 256 KiB", "256", "83,751", "167,471"]]
        table(s, X0, TOP + 0.42, [1.05, 1.65, 0.95, 1.1, 1.25], rows, "workloads table",
              row_h=0.4, align="llrrr", bold_first=True)
        items = [("Rotation.", "Every drive runs large, medium, and small files in turn, so "
                  "every failure is tied to a known file size."),
                 ("Content.", "Each file is generated in memory from a 64-bit key, so the "
                  "expected data can be regenerated at any time.")]
        prose(s, X0, TOP + 2.2, 6.0, items, size=TEXT, gap=8)
        x, w = 7.2, RIGHT - 7.2
        heading(s, x, TOP, w, "Every 1 MiB chunk identifies itself", name="chunk")
        y = TOP + 0.5
        hw = 1.55
        head = box(s, x, y, hw, 0.6, "chunk header", fill=NAVY, radius=0.1)
        head.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        tb(s, x, y, hw, 0.6, [[("32-byte header", {"size": NOTE, "bold": True,
                                                   "color": WHITE})]],
           name="chunk header text", align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE,
           spacing=1.0)
        box(s, x + hw + 0.06, y, w - hw - 0.06, 0.6, "chunk payload")
        tb(s, x + hw + 0.06, y, w - hw - 0.06, 0.6, [[("SHAKE-128 output, incompressible",
                                                       {"size": NOTE, "color": BODY})]],
           name="chunk payload text", align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE,
           spacing=1.0)
        tb(s, x, y + 0.68, w, 0.3, [[("file key  ·  cycle  ·  file number  ·  chunk number",
                                      {"size": NOTE, "color": MUTED})]],
           name="chunk fields", spacing=1.0)
        heading(s, x, TOP + 1.55, w, "A chunk that reads back wrong shows",
                f"after the self-describing records of Zheng et al. "
                f"{self.cite('zheng2013understanding')}", name="kinds")
        rows = [["Kind", "What the header shows"],
                ["bit errors", "sparse flipped bits in the right chunk"],
                ["stale data", "a chunk from an earlier cycle"],
                ["misdirected", "a chunk of another file"],
                ["erased page", "an erased page instead of data"],
                ["truncation", "the file ends early"]]
        table(s, x, TOP + 2.26, [1.6, w - 1.6], rows, "kinds table", head_h=0.34, row_h=0.36,
              size=13, bold_first=True)
        self.notes(s, lead, *[f"{h} {b}" for h, b in items],
                   "Every 1 MiB chunk starts with a header, so a wrong chunk shows whether the "
                   "drive returned bit errors, stale data, another file, an erased page, or a "
                   "truncated file.")

    def cycle(self):
        lead = ("Each cycle writes, reads back, and deletes one file set. An error that persists "
                "starts a recovery check.")
        s = self.slide("Method", "The Test Cycle", lead)
        path = self.figs / "fig_cycle.png"
        pw, ph = image_size(path)
        h = 4.25
        w = h * pw / ph
        place(s, path, X0 + (W - w) / 2, 2.22, h=h, name="cycle figure",
              alt="Flowchart of one test cycle and of the failure handling")
        self.notes(s, lead, "The top row is one cycle. The worker finds its drive, plans the "
                   "cycle, writes, reads back, deletes, and logs. The blue boxes are the timed "
                   "phases. An error is retried twice. If it persists, the recovery check "
                   "decides whether the drive keeps cycling or stops until the post-test "
                   "check.")

    def directions(self):
        lead = "Each cycle tests both directions of transfer and times every phase separately."
        s = self.slide("Method", "Write, Read Back, and Delete", lead)
        cards = [("Write", "host to drive", "Every file is written chunk by chunk and flushed "
                  "to the drive before the next file starts."),
                 ("Read back", "drive to host", "Files are read with "
                  "{{FILE_FLAG_NO_BUFFERING}}, so every read reaches the drive, and each chunk "
                  "is compared byte by byte."),
                 ("Delete", "on the drive", "The files are removed, and the free space must "
                  "return to its value before the cycle (F6).")]
        cw, gap, ch = (W - 0.5) / 3, 0.25, 2.3
        for i, (head, sub, body) in enumerate(cards):
            card(s, X0 + i * (cw + gap), TOP, cw, ch, head, body, name=f"phase {i}",
                 number=f"{i + 1:02d}", sub=sub, size=SMALL)
        y = TOP + ch + 0.25
        box(s, X0, y, W, BOTTOM - y, "retries")
        box(s, X0, y, 0.08, BOTTOM - y, "retries bar", fill=CRIMSON, shape=MSO_SHAPE.RECTANGLE)
        retry = ("Every error is retried twice, after 5 s and 10 s. A file that does not match "
                 "is read again. A match then counts as a transient error (D3), and a second "
                 "mismatch as data corruption (F1).")
        tb(s, X0 + 0.35, y + 0.18, W - 0.6, BOTTOM - y - 0.3, [
            [("Errors and retries", {"size": SMALL, "bold": True, "color": NAVY})],
            [(retry, {"size": SMALL})]], name="retries text", spacing=1.12, space_after=4)
        self.notes(s, lead, *[f"{h}, {sub}. {b}" for h, sub, b in cards], retry)

    def taxonomy(self):
        lead = ("A cycle that ends in F1 to F4 or F6 is a cycle failure. D1 to D3 are warning "
                "signs.")
        s = self.slide("Method", "Failure Modes and Degradation Indicators", lead)
        rows = [["Code", "Name", "Definition", "Automatic detection"],
                ["F1", "Data corruption", "read-back differs from the data written, including "
                 "missing or truncated files", "byte-exact comparison, mismatch on a second "
                 "read"],
                ["F2", "I/O error", "a read or write error that two retries do not clear",
                 "error codes of the operating system"],
                ["F3", "Disconnect or hang", "the drive leaves the bus or makes no progress",
                 "volume check in every phase, watchdog after 10 min"],
                ["F4", "Read-only lock", "the drive refuses writes", "write-protect error, "
                 "read-only flag of the volume"],
                ["F5", "Port or host fault", "the dock, the port, or the computer causes the "
                 "failure", "the spare drive also fails on that port"],
                ["F6", "File-system fault", "damaged volume, space not returned, or capacity "
                 "change", "free-space accounting in every cycle"],
                ["D1", "Slowdown", "write speed below 50 % of the baseline of the drive",
                 "three passing cycles in a row below it"],
                ["D2", "Intermittent failure", "a cycle failure after which the drive works "
                 "again", "the drive passes the recovery check"],
                ["D3", "Transient error", "an error that a retry within the cycle clears",
                 "the retry succeeds or the second read matches"]]
        gf = table(s, X0, TOP, [0.75, 2.2, 4.45, W - 7.4], rows, "taxonomy table",
                   row_h=0.43, size=13, bold_first=True)
        for r in range(1, len(rows)):  # failure modes in the accent, indicators in navy
            for run in gf.table.cell(r, 0).text_frame.paragraphs[0].runs:
                run.font.color.rgb = CRIMSON if rows[r][0].startswith("F") else NAVY
        self.notes(s, lead, "F1 to F6 are data corruption, input/output errors, disconnects and "
                   "hangs, read-only locks, port or host faults, and file-system faults. D1 to "
                   "D3 are a slowdown below half the baseline, an intermittent failure, and a "
                   "transient error that a retry clears. Each one has an automatic rule, shown "
                   "on the right.")

    def protocol(self):
        lead = ("A recovery check and a post-test check with a spare drive decide what each "
                "cycle failure means.")
        s = self.slide("Method", "Recovery and Attribution Protocol", lead)
        steps = [("During the test", "After a cycle failure, the worker waits up to 120 s and "
                  "runs a 64 MiB write, verify, and delete check. A drive that passes keeps "
                  "cycling, and the failure is soft (D2)."),
                 ("After the stop", "A drive that failed the check is tested on another port. "
                  "If it fails again, it has failed hard at cycle %k%."),
                 ("Port check", "If it passes, the spare A8-05 tests the old port. If the spare "
                  "also fails, the failure is a port fault (F5), and the drive is censored.")]
        y, w = TOP, 6.45
        for i, (head, body) in enumerate(steps):
            h = 0.8 + 0.23 * lines_of(body, w - 0.45, SMALL, em=0.52)
            card(s, X0, y, w, h, head, body, name=f"step {i}", number=f"{i + 1:02d}")
            y += h + 0.12
        x, w = 7.55, RIGHT - 7.55
        heading(s, x, TOP, w, "A drive has failed hard when it", name="hard")
        hard = ["fails the recovery check and then fails on another port,",
                "locks into read-only mode,",
                "reports a changed capacity, or",
                "has three cycle failures within ten cycles."]
        yy = TOP + 0.45
        for i, item in enumerate(hard):
            hairline(s, x, yy, w, f"hard rule {i}")
            tb(s, x, yy + 0.08, w, 0.3, [[(item, {"size": SMALL})]], name=f"hard {i}",
               spacing=1.0)
            yy += 0.42
        life = ("Life %T% is the cycle %k% of the hard failure. Drives still running at the stop "
                "are right-censored. Failures of two or more drives on one path within 10 min "
                "go through the port check first.")
        box(s, x, yy + 0.2, w, BOTTOM - yy - 0.2, "life box")
        tb(s, x + 0.25, yy + 0.38, w - 0.5, BOTTOM - yy - 0.5, [[(life, {"size": SMALL})]],
           name="life text", spacing=1.12)
        self.notes(s, lead, *[f"{h}. {b}" for h, b in steps])

    def responses(self):
        lead = "The test records one primary response and six more for early failure and degradation."
        s = self.slide("Method", "Responses and Covariates", lead)
        rows = [["", "Response", "Recorded"],
                ["R1", "cycles to hard failure, %T%", "per drive"],
                ["R2", "cycles to the first cycle failure", "per drive"],
                ["R3", "cycles at which soft failures occur", "per event"],
                ["R4", "write and read throughput (MB/s)", "per cycle"],
                ["R5", "time per file: median, 95th percentile, maximum", "per cycle"],
                ["R6", "corrupted files, bytes, and bits, and the kind", "per cycle"],
                ["R7", "transient errors (D3)", "per cycle"]]
        rows = [rows[0]] + [[r[0], [(p, o) for p, o in runs_of(r[1], {})], r[2]]
                            for r in rows[1:]]
        table(s, X0, TOP, [0.6, 4.75, 1.4], rows, "responses table", row_h=0.5, size=14,
              bold_first=True)
        items = [("Primary.", "R1 is the primary response."),
                 ("Early failures.", "R2 and R3 capture early and intermittent failures."),
                 ("Degradation.", "R4 to R7 are the degradation measurements of every cycle."),
                 ("Covariates.", "Brand, capacity, and housing are fixed per drive, and the "
                  "workload changes with every cycle. Every cycle record stores the host, the "
                  "port, and the data written so far.")]
        prose(s, 7.6, TOP, RIGHT - 7.6, items)
        self.notes(s, lead, *[f"{b}" for _, b in items])

    # -- design ---------------------------------------------------------------------------
    def factors(self):
        lead = "Because the file size rotates, eight drives cover brand, capacity, and file size together."
        s = self.slide("Design", "Factors and Allocation", lead)
        heading(s, X0, TOP, 6.4, "Factors", name="factors")
        rows = [["Factor", "Levels", "Drives", "Changes"],
                ["Brand", "SanDisk, ABLAZE (8 GB)", "2 vs. 4", "per drive"],
                ["Capacity", "8 GB, 16 GB (ABLAZE)", "4 vs. 2", "per drive"],
                ["File size", "small, medium, large", "all 8", "per cycle"],
                ["Connection", "desktop port, dock", "6 vs. 2", "recorded"]]
        table(s, X0, TOP + 0.42, [1.5, 2.55, 1.05, 1.3], rows, "factors table", row_h=0.5,
              size=14, bold_first=True)
        items = [("Own control.", "Every drive runs all three file sizes, so comparing the "
                  "workloads within the same drives removes differences in drive quality."),
                 ("Balance.", "All drives run the same mix of workloads, which keeps the brand "
                  "and capacity comparisons balanced."),
                 ("Fixed conditions.", "One host, one file system, and room temperature for "
                  "all drives. The dock carries one S8 and one A16 drive.")]
        prose(s, 7.55, TOP, RIGHT - 7.55, items)
        self.notes(s, lead, *[f"{h} {b}" for h, b in items])

    def duration(self):
        lead = ("At the planning speeds an 8 GB drive completes about 24 cycles in one day, and "
                "a 16 GB drive about 12.")
        s = self.slide("Design", "Test Duration and Feasibility", lead)
        eq = self.figs / "eq_cycle.png"
        pw, _ = image_size(eq)
        place(s, eq, X0, TOP - 0.05, w=pw / EQ_DPI, name="cycle time equation",
              alt="t_c = V/w + V/r + t_0")
        items = ["A cycle moves %V% = 0.93 %φC% bytes each way, with fill fraction %φ% = 0.9, "
                 "capacity %C%, write and read speeds %w% and %r%, and overhead %t%_{0} = 60 s.",
                 "We plan with write speeds of 1, 6, and 7 MB/s and read speeds of 6, 18, and "
                 "20 MB/s for small, medium, and large files. The pilot replaces them with "
                 "measured values."]
        bottom = prose(s, X0, TOP + 0.8, 6.2, items, size=SMALL + 0.5, gap=7, to=4.9)
        rows = [["Drive", "GB per cycle", "Hours per cycle", "Cycles in 24 h",
                 "Write speed halved"],
                ["8 GB", "6.7", "1.00", "24", "13 cycles"],
                ["16 GB", "13.4", "1.98", "12", "7 cycles"]]
        table(s, X0, max(bottom + 0.05, 4.95), [0.8, 1.2, 1.5, 1.3, 1.4], rows,
              "feasibility table", row_h=0.4, align="lrrrr", bold_first=True)
        x, w = 7.35, RIGHT - 7.35
        heading(s, x, TOP, w, "Full-fill cycles per day", "against the sustained write speed, "
                "read speed 20 MB/s", name="rate")
        path = self.figs / "panel_rate.png"
        pw, ph = image_size(path)
        fw = min(w, 3.55 * pw / ph)
        place(s, path, x + (w - fw) / 2, TOP + 0.75, w=fw, name="rate figure",
              alt="Full-fill cycles per day against the sustained write speed")
        self.notes(s, lead, *items, "Table VII gives the cycle times. At half the write speed, "
                   "the counts drop to 13 and 7 cycles.")

    def expected(self):
        lead = ("Hard failures are likely only from weak drives, so the plan also bounds life "
                "when no drive fails.")
        s = self.slide("Design", "Expected Failures and the Lower Bound", lead)
        w = (W - 0.5) / 2
        panels = [("failures", "Expected hard failures in 24 h",
                   "Weibull shape γ = 2 (line), 1.5 to 3 (band)",
                   "For γ = 2, eight drives give about three failures if θ = 30 cycles, 0.4 if "
                   "θ = 100, and almost none if θ = 300. Earlier tests saw failures after 100 "
                   "to 1,000 full-drive writes."),
                  ("bound", "Lower bound on θ if no drive fails",
                   "Weibayes bound, eight drives pooled, 90 % confidence",
                   "With no failure in one day, the test shows θ ≥ 40 cycles for γ = 2, 34 for "
                   "γ = 3, and 49 for γ = 1.5. The bound grows with the test length.")]
        for i, (name, head, sub, text) in enumerate(panels):
            x = X0 + i * (w + 0.5)
            heading(s, x, TOP, w, head, sub, name=f"panel {i}")
            path = self.figs / f"panel_{name}.png"
            pw, ph = image_size(path)
            fh = 2.7
            fw = fh * pw / ph
            place(s, path, x + (w - fw) / 2, TOP + 0.72, h=fh, name=f"panel {i} figure",
                  alt=head)
            tb(s, x, TOP + 0.72 + fh + 0.12, w, 0.75, [[(text, {"size": SMALL})]],
               name=f"panel {i} text", spacing=1.1)
        self.notes(s, lead, panels[0][3], panels[1][3])

    def references(self):
        size, per_col = 10, 11
        pages = [self.refs[i:i + 2 * per_col] for i in range(0, len(self.refs), 2 * per_col)]
        cw = (W - 0.3) / 2
        for k, keys in enumerate(pages):
            s = self.slide(None, "References" if k == 0 else "References (continued)", None)
            base = 1 + k * 2 * per_col
            for c, col in enumerate((keys[:per_col], keys[per_col:])):
                if not col:
                    continue
                first = base + c * per_col
                paras = [[(f"[{first + i}]\t", {"size": size, "color": MUTED}),
                          *[(text, {"size": size, "italic": it}) for text, it in REFS[key]]]
                         for i, key in enumerate(col)]
                shape = tb(s, X0 + c * (cw + 0.3), 1.62, cw, 5.1, paras,
                           name=f"reference list {c}", spacing=1.0, space_after=2.5)
                for p in shape.text_frame.paragraphs:  # hanging indent past the number
                    pPr = p._p.get_or_add_pPr()
                    pPr.set("marL", str(int(Inches(0.42))))
                    pPr.set("indent", str(-int(Inches(0.42))))
            self.notes(s, "The references cited on the slides, all from the bibliography of "
                          "the test plan.")

    # -- build ----------------------------------------------------------------------------
    def build(self, out: Path) -> Path:
        self.restyle()
        self.cover_slide()
        self.closing_slide()
        body = [self.intro, self.wear, self.ftl, self.standards, self.usb_tests,
                self.conditions, self.lessons, self.field, self.statistics, self.gap,
                self.drives, self.rig, self.software, self.workloads, self.cycle,
                self.directions, self.taxonomy, self.protocol, self.responses, self.factors,
                self.duration, self.expected, self.references]
        for make in body:
            make()
        added = list(self.prs.slides)[2:]
        lst = self.prs.slides._sldIdLst
        ids = {s.slide_id: el for s, el in zip(self.prs.slides, list(lst))}
        wanted = [ids[s.slide_id] for s in [self.cover, *added, self.closing]]
        for el in list(lst):
            lst.remove(el)
        for el in wanted:
            lst.append(el)
        for i, s in enumerate(self.prs.slides, 1):
            s.part.partname = PackURI(f"/ppt/slides/slide{i}.xml")
            for fld in s._element.iter(qn("a:fld")):
                if fld.get("type") == "slidenum" and fld.find(qn("a:t")) is not None:
                    fld.find(qn("a:t")).text = str(i)
        out.parent.mkdir(parents=True, exist_ok=True)
        self.prs.save(str(out))
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--template", required=True, help="the first proposal deck (design source)")
    ap.add_argument("--figures", required=True, help="folder for the slide figures")
    ap.add_argument("--qr", help="QR code of the repository (PNG)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-figures", action="store_true", help="reuse the drawn figures")
    args = ap.parse_args(argv)
    figures = Path(args.figures)
    if not args.no_figures:
        draw_figures(figures)
    deck = ProposalDeck(Path(args.template), figures, Path(args.qr) if args.qr else None)
    out = deck.build(Path(args.out))
    print(f"wrote {out} ({len(deck.prs.slides)} slides, {len(deck.refs)} references)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
