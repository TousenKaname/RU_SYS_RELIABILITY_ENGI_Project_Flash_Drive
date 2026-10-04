"""One visual language for every figure.

Typography and line weights follow the figure guidelines of Nature-family
journals: a sans-serif face at 7 pt, 0.6 pt axes, no top or right spines,
lower-case bold panel letters, and no titles inside the plot. Colour encodes
meaning rather than decoration: brand is hue (SanDisk blue, ABLAZE amber) and
capacity is lightness (ABLAZE 8 GB light, 16 GB dark), so the figures also
read in greyscale and for colour-blind readers. Vermilion is reserved for
failures and thresholds.

Widths match the IEEE two-column layout (3.5 in column, 7.16 in page).
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt

MM = 1 / 25.4
COLUMN_W = 3.5      # inches, one IEEE column
PAGE_W = 7.16       # inches, full IEEE text width

INK = "#222222"
GRAY = "#6F6F6F"
MIST = "#B9B9B9"
HAIRLINE = "#DADADA"
PAPER = "#F4F4F2"
FAIL = "#C4452F"     # vermilion
PASS = "#3F7F5F"     # muted green

GROUP_COLORS = {
    "S8": "#4A7FB0",   # SanDisk 8 GB, mid blue
    "A8": "#D9973F",   # ABLAZE 8 GB, light amber
    "A16": "#8C5320",  # ABLAZE 16 GB, dark amber
}
GROUP_LABELS = {
    "S8": "SanDisk 8 GB",
    "A8": "ABLAZE 8 GB",
    "A16": "ABLAZE 16 GB",
}
GROUP_MARKERS = {"S8": "o", "A8": "s", "A16": "^"}
WORKLOAD_COLORS = {"small": "#9CC9BA", "medium": "#4E9C86", "large": "#1F5F51"}

RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
    "font.size": 7,
    "axes.labelsize": 7,
    "axes.titlesize": 7,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.6,
    "axes.edgecolor": INK,
    "axes.labelcolor": INK,
    "axes.labelpad": 2.5,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": False,
    "xtick.color": INK,
    "ytick.color": INK,
    "xtick.direction": "out",
    "ytick.direction": "out",
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.minor.width": 0.45,
    "ytick.minor.width": 0.45,
    "xtick.major.size": 2.6,
    "ytick.major.size": 2.6,
    "xtick.minor.size": 1.5,
    "ytick.minor.size": 1.5,
    "xtick.major.pad": 2.0,
    "ytick.major.pad": 2.0,
    "lines.linewidth": 1.1,
    "lines.markersize": 3.2,
    "lines.markeredgewidth": 0.6,
    "patch.linewidth": 0.6,
    "legend.frameon": False,
    "legend.handlelength": 1.6,
    "legend.handletextpad": 0.5,
    "legend.borderaxespad": 0.3,
    "legend.labelspacing": 0.35,
    "text.color": INK,
    "mathtext.fontset": "custom",
    "mathtext.rm": "Arial",
    "mathtext.it": "Arial:italic",
    "mathtext.bf": "Arial:bold",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    "figure.dpi": 150,
    "savefig.dpi": 600,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
}


def apply() -> None:
    """Install the style for every figure that follows."""
    mpl.rcParams.update(RC)


def text_on(fill: str) -> str:
    """Ink or white, whichever reads better on ``fill`` (WCAG relative luminance)."""
    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    luminance = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return INK if luminance > 0.28 else "white"


def panel_label(ax, letter: str, x: float = -0.16, y: float = 1.04) -> None:
    """Lower-case bold panel letter at the top-left corner, outside the axes."""
    ax.text(x, y, letter, transform=ax.transAxes, fontsize=8, fontweight="bold",
            va="bottom", ha="left", color=INK)


def fit_width(fig, width_in: float, pad_in: float = 0.02, rounds: int = 6):
    """Resize ``fig`` so that its tight bounding box is exactly ``width_in`` wide.

    LaTeX can then place the figure at 1:1 scale, so the 7 pt type stays 7 pt.
    Text keeps its size while the axes grow or shrink, hence the iteration.
    """
    for _ in range(rounds):
        fig.canvas.draw()
        box = fig.get_tightbbox(fig.canvas.get_renderer()).padded(pad_in)
        error = width_in - box.width
        if abs(error) < 0.002:
            break
        w, h = fig.get_size_inches()
        fig.set_size_inches(w + error, h)
    fig.canvas.draw()
    return fig.get_tightbbox(fig.canvas.get_renderer()).padded(pad_in)


def save(fig, stem: str | Path, formats: tuple[str, ...] = ("pdf", "png"),
         width: float | None = None) -> list[Path]:
    """Save ``stem.pdf`` (vector, for LaTeX) and ``stem.png`` (preview).

    With ``width`` (inches) the saved figure is exactly that wide, e.g. ``PAGE_W``.
    """
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    bbox = fit_width(fig, width) if width else "tight"
    paths = []
    for fmt in formats:
        path = stem.with_suffix("." + fmt)
        fig.savefig(path, bbox_inches=bbox)
        paths.append(path)
    plt.close(fig)
    return paths
