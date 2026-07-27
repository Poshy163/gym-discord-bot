"""Render candidate looks for the /bodyweight_graph and /graph PNGs.

Throwaway comparison harness. Uses a weigh-in series matching a real 16-entry
run (110kg → 106.2kg, peak 110.9) so the trade-offs show on realistic data
rather than a smooth curve.

    python scripts/preview_graph_options.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates       # noqa: E402
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.ticker as ticker       # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "previews"

# 16 weigh-ins over ~10 weeks, matching the shape of the real chart.
START = date(2026, 5, 9)
SERIES = [
    (0, 110.0), (3, 110.9), (5, 109.9), (8, 109.6), (12, 109.6),
    (36, 110.5), (38, 110.3), (41, 108.9), (43, 108.85), (45, 110.9),
    (54, 107.7), (58, 108.5), (63, 109.3), (70, 106.9), (73, 106.9),
    (77, 106.2),
]
XS = [START + timedelta(days=d) for d, _w in SERIES]
YS = [w for _d, w in SERIES]
GOAL = 100.0

# Discord dark, so the PNG sits inside the embed instead of glaring out of it.
BG = "#2b2d31"       # embed body
PANEL = "#313338"    # chat background
INK = "#dbdee1"
MUTED = "#949ba4"
GRID = "#3f4147"
BRAND = "#f26522"
GOOD = "#57f287"
BAD = "#ed4245"


def rolling(values, window=3):
    out = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        chunk = values[lo:i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _frame(dark=True):
    fig, ax = plt.subplots(figsize=(8.8, 4.4), dpi=150)
    if dark:
        fig.patch.set_facecolor(BG)
        ax.set_facecolor(BG)
        ax.tick_params(colors=MUTED, labelsize=9)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", color=GRID, linewidth=0.8, alpha=0.7)
    ax.set_axisbelow(True)
    return fig, ax


def _titles(ax, title, subtitle, colour=INK):
    ax.set_title(title, loc="left", fontsize=15, fontweight="bold",
                 color=colour, pad=20)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9,
            color=MUTED, va="bottom")


def _dates(ax):
    """Concise date ticks with the year offset suppressed.

    ConciseDateFormatter parks a "2026-Jul" offset label in the bottom-right
    corner by default, which reads as a stray annotation. The span is already
    in the subtitle.
    """
    loc = mdates.AutoDateLocator(minticks=4, maxticks=7)
    fmt = mdates.ConciseDateFormatter(loc)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(fmt)
    # The offset text is the "2026-Jul" that ends up parked in the corner.
    ax.xaxis.get_offset_text().set_visible(False)
    span = XS[-1] - XS[0]
    pad = timedelta(days=max(1, int(span.days * 0.04)))
    ax.set_xlim(XS[0] - pad, XS[-1] + pad)


def _ylim(ax, extra=()):
    lo, hi = min(list(YS) + list(extra)), max(list(YS) + list(extra))
    pad = max(0.8, (hi - lo) * 0.18)
    ax.set_ylim(lo - pad, hi + pad)
    ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=5))


def _endpoints(ax, colour):
    for idx, va, dy in ((0, "bottom", 10), (len(XS) - 1, "top", -12)):
        ax.annotate(
            f"{YS[idx]:g}kg", xy=(XS[idx], YS[idx]), xytext=(0, dy),
            textcoords="offset points", ha="center", va=va,
            fontsize=9, fontweight="bold", color=colour,
        )


def _save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    fig.tight_layout(pad=1.1)
    fig.savefig(path, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {path}")


# --------------------------------------------------------------------------
# A — same chart, Discord dark
# --------------------------------------------------------------------------

def option_a():
    fig, ax = _frame()
    ax.plot(XS, YS, marker="o", markersize=6, markerfacecolor=BG,
            markeredgewidth=2, linewidth=2.4, color=BRAND)
    _titles(ax, "Poshy — bodyweight",
            "16 entries · 80 days · 110kg → 106.2kg (−3.8kg)")
    ax.set_ylabel("kg", color=MUTED)
    _ylim(ax)
    _dates(ax)
    _endpoints(ax, INK)
    _save(fig, "graph-a-dark.png")


# --------------------------------------------------------------------------
# B — trend line: the noise recedes, the trajectory reads
# --------------------------------------------------------------------------

def option_b():
    fig, ax = _frame()
    trend = rolling(YS, 3)
    net = YS[-1] - YS[0]
    colour = GOOD if net < 0 else BAD          # losing weight is the goal here
    ax.plot(XS, YS, marker="o", markersize=4.5, markerfacecolor=BG,
            markeredgewidth=1.4, linewidth=1.1, color=MUTED, alpha=0.75,
            label="weigh-ins", zorder=2)
    ax.plot(XS, trend, linewidth=3.0, color=colour, label="3-point trend",
            zorder=3, solid_capstyle="round")
    ax.fill_between(XS, trend, min(YS) - 3, color=colour, alpha=0.09, zorder=1)
    _titles(ax, "Poshy — bodyweight",
            "16 entries · 80 days · 110kg → 106.2kg (−3.8kg)")
    ax.set_ylabel("kg", color=MUTED)
    _ylim(ax)
    _dates(ax)
    _endpoints(ax, INK)
    leg = ax.legend(loc="upper right", frameon=False, fontsize=8.5)
    for t in leg.get_texts():
        t.set_color(MUTED)
    _save(fig, "graph-b-trend.png")


# --------------------------------------------------------------------------
# C — goal line: shows the distance left, not just the history
# --------------------------------------------------------------------------

def option_c():
    fig, ax = _frame()
    trend = rolling(YS, 3)
    ax.fill_between(XS, YS, GOAL, color=BRAND, alpha=0.07, zorder=1)
    ax.plot(XS, YS, marker="o", markersize=4.5, markerfacecolor=BG,
            markeredgewidth=1.4, linewidth=1.1, color=MUTED, alpha=0.7,
            zorder=2)
    ax.plot(XS, trend, linewidth=3.0, color=GOOD, zorder=3,
            solid_capstyle="round")
    ax.axhline(GOAL, color=GOOD, linewidth=1.6, linestyle=(0, (5, 4)),
               alpha=0.9, zorder=2)
    ax.annotate(f"goal {GOAL:g}kg", xy=(XS[0], GOAL), xytext=(2, 5),
                textcoords="offset points", fontsize=9, color=GOOD,
                fontweight="bold")
    _titles(ax, "Poshy — bodyweight",
            f"16 entries · 80 days · 110kg → 106.2kg (−3.8kg) · "
            f"{YS[-1] - GOAL:.1f}kg to goal")
    ax.set_ylabel("kg", color=MUTED)
    _ylim(ax, extra=[GOAL])
    _dates(ax)
    _endpoints(ax, INK)
    _save(fig, "graph-c-goal.png")


# --------------------------------------------------------------------------
# D — headline number, chart as supporting evidence
# --------------------------------------------------------------------------

def option_d():
    fig = plt.figure(figsize=(8.8, 4.4), dpi=150)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0.06, 0.12, 0.88, 0.52])
    ax.set_facecolor(BG)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.grid(False)
    ax.set_yticks([])
    ax.tick_params(colors=MUTED, labelsize=9)

    trend = rolling(YS, 3)
    ax.plot(XS, YS, linewidth=0.9, color=MUTED, alpha=0.5, zorder=2)
    ax.plot(XS, trend, linewidth=3.2, color=GOOD, zorder=3,
            solid_capstyle="round")
    ax.fill_between(XS, trend, min(YS) - 2, color=GOOD, alpha=0.10, zorder=1)
    ax.scatter([XS[-1]], [YS[-1]], s=70, color=GOOD, zorder=4,
               edgecolor=BG, linewidth=2)
    _ylim(ax)
    _dates(ax)

    fig.text(0.06, 0.90, "Poshy — bodyweight", fontsize=13,
             fontweight="bold", color=INK, va="top")
    fig.text(0.06, 0.755, f"{YS[-1]:g}", fontsize=42, fontweight="bold",
             color=INK, va="center")
    fig.text(0.175, 0.745, "kg", fontsize=15, color=MUTED, va="center")
    fig.text(0.245, 0.775, "−3.8kg", fontsize=15, fontweight="bold",
             color=GOOD, va="center")
    fig.text(0.245, 0.715, "since 9 May", fontsize=9, color=MUTED,
             va="center")
    fig.text(0.42, 0.775, "6.2kg", fontsize=15, fontweight="bold",
             color=INK, va="center")
    fig.text(0.42, 0.715, "to goal", fontsize=9, color=MUTED, va="center")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "graph-d-headline.png"
    fig.savefig(path, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {path}")


if __name__ == "__main__":
    option_a()
    option_b()
    option_c()
    option_d()
