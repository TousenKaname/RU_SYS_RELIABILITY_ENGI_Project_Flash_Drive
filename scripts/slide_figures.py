"""Slide copies of three report figures, for the presentation (``make_presentation.py``).

    python scripts/slide_figures.py configs/campaign.yaml --out ../Project/results/slides

The report draws its figures at the IEEE page or column width with 6 to 7 pt type. A slide
shows a figure about twice as large, so these copies are drawn at about half the width
with the same type, which then reads at 12 to 13 pt on the slide. The drawing code is the
report's own (``results_figures.py`` and ``forensics.py``); only the size changes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import matplotlib

matplotlib.use("Agg")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forensics  # noqa: E402
from make_results import drive_summary, load, milestones  # noqa: E402
from results_figures import cut_short_cycles, fig_context, fig_timeline  # noqa: E402

from flashrel.config import load_campaign  # noqa: E402
from flashrel.viz import style  # noqa: E402

#: Figure sizes in inches, drawn at 6 to 7 pt and placed on the slide at about 2.1 times,
#: so that the smallest type (5.6 pt in fig_context) reads at 12 pt.
TIMELINE = (5.6, 1.72)
FORENSICS = (6.2, 1.72)
CONTEXT = (2.9, 1.78)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("config")
    ap.add_argument("--out", required=True)
    ap.add_argument("--forensics", help="folder with forensics.json (default: the run's)")
    args = ap.parse_args(argv)
    campaign = load_campaign(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    style.apply()
    cycles, life, events = load(campaign)
    marks = milestones(campaign, cycles)
    drives = drive_summary(campaign, cycles, life, events)
    written = fig_timeline(campaign, cycles, life, marks, out,
                           cut_short_cycles(cycles, events, marks), size=TIMELINE,
                           name="slide_timeline", legend_cols=4)
    written += fig_context(campaign, drives, out, size=CONTEXT, name="slide_context",
                           legend_cols=3)
    data = Path(args.forensics) if args.forensics else Path(campaign.run_dir) / "forensics"
    forensics.figure(SimpleNamespace(config=args.config, data=str(data), out=str(out),
                                     width=FORENSICS[0], height=FORENSICS[1],
                                     name="slide_forensics", no_tex=True, ratio_b=1.35))
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
