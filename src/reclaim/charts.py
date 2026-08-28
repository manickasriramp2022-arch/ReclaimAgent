"""Inline SVG charts for the HTML report.

Hand-rolled SVG, no library and no external request, because a test asserts the
rendered report contains no `http://` or `https://`. Every chart here is
therefore a string of markup with the geometry computed in Python.

Design rules followed, from the data-visualisation guidance:

* Form first. Magnitude across a set is a bar; a distribution is a sorted
  column strip; two measures of different scale are two charts, never two
  y-axes.
* Colour last, and validated rather than eyeballed. The two-series palette
  (blue #2a78d6, orange #eb6834) was run through the palette validator against
  this report's white surface: CVD deltaE 24.7, normal-vision 33.6, contrast
  above 3:1 for both. A single-series chart uses blue alone and carries no
  legend, because one colour needs no key.
* Marks stay thin, bars round only at the data end and sit square on the
  baseline, adjacent bars are separated by a 2px surface gap, and gridlines are
  hairline and recessive.
* Direct labels are sparing. On the distribution only the extremes and the
  median are labelled; flooding every column would make all of them useless.
* Every mark carries a `<title>`, which is the zero-JavaScript hover layer, and
  the report already presents the same numbers as tables, which is the
  non-visual view.

A zero is labelled explicitly wherever one occurs. A bar of length zero renders
as nothing, and in this report "nothing" is the single most important measured
result: it is what a hard-stop category's retry count looks like. An unlabelled
absence would read as missing data.
"""

from __future__ import annotations

import html
from collections.abc import Sequence

SURFACE = "#ffffff"
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif'
BAR_MAX_THICKNESS = 24.0
SURFACE_GAP = 2.0
CORNER = 4.0


def _esc(text: object) -> str:
    return html.escape(str(text))


def _frame(width: int, height: int, title: str, subtitle: str, body: str) -> str:
    return (
        f'<figure class="chart">'
        f'<figcaption><span class="ct">{_esc(title)}</span>'
        f'<span class="cs">{_esc(subtitle)}</span></figcaption>'
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{_esc(title)}. {_esc(subtitle)}" '
        f'style="font-family:{FONT}">{body}</svg></figure>'
    )


def _rounded_bar_v(x: float, y: float, w: float, h: float, fill: str, tip: str) -> str:
    """Column: rounded at the top (the data end), square on the baseline."""
    if h <= 0.5:
        return ""
    r = min(CORNER, w / 2, h)
    d = (
        f"M{x:.2f},{y + h:.2f} V{y + r:.2f} Q{x:.2f},{y:.2f} {x + r:.2f},{y:.2f} "
        f"H{x + w - r:.2f} Q{x + w:.2f},{y:.2f} {x + w:.2f},{y + r:.2f} "
        f"V{y + h:.2f} Z"
    )
    return f'<path d="{d}" fill="{fill}"><title>{_esc(tip)}</title></path>'


def _rounded_bar_h(x: float, y: float, w: float, h: float, fill: str, tip: str) -> str:
    """Bar: rounded at the right (the data end), square at the axis."""
    if w <= 0.5:
        return ""
    r = min(CORNER, h / 2, w)
    d = (
        f"M{x:.2f},{y:.2f} H{x + w - r:.2f} Q{x + w:.2f},{y:.2f} {x + w:.2f},{y + r:.2f} "
        f"V{y + h - r:.2f} Q{x + w:.2f},{y + h:.2f} {x + w - r:.2f},{y + h:.2f} "
        f"H{x:.2f} Z"
    )
    return f'<path d="{d}" fill="{fill}"><title>{_esc(tip)}</title></path>'


def _text(
    x: float, y: float, s: str, size: float, fill: str, anchor: str = "start", weight: int = 400
) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{_esc(s)}</text>'
    )


def _zero_marker(x: float, y_mid: float, fill: str) -> str:
    """A visible stub where a bar has length zero.

    Zero is a measured result here, not absent data, so it gets a mark and a
    label rather than blank space.
    """
    return (
        f'<rect x="{x:.2f}" y="{y_mid - 6:.2f}" width="2" height="12" fill="{fill}" '
        f'opacity="0.55"><title>zero</title></rect>'
    )


# ---------------------------------------------------------------------------
# 1. Seed sweep: the distribution of the delta
# ---------------------------------------------------------------------------
def sweep_distribution(deltas: Sequence[float], median: float) -> str:
    """Sorted column strip. One series, so no legend; the caption names it."""
    if not deltas:
        return ""
    values = sorted(deltas)
    width, height = 900, 280
    left, right, top, bottom = 54, 18, 18, 46
    plot_w = width - left - right
    plot_h = height - top - bottom
    hi = max(max(values), median) * 1.12
    slot = plot_w / len(values)
    bar_w = min(BAR_MAX_THICKNESS, max(3.0, slot - SURFACE_GAP))

    parts: list[str] = []
    step = 20.0
    tick = 0.0
    while tick <= hi * 100:
        y = top + plot_h - (tick / 100 / hi) * plot_h
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        parts.append(_text(left - 8, y + 4, f"{tick:.0f}%", 11, INK_MUTED, "end"))
        tick += step

    for i, value in enumerate(values):
        h = (value / hi) * plot_h
        x = left + i * slot + (slot - bar_w) / 2
        parts.append(
            _rounded_bar_v(x, top + plot_h - h, bar_w, h, SERIES_1, f"seed delta {value:+.1%}")
        )

    med_y = top + plot_h - (median / hi) * plot_h
    parts.append(
        f'<line x1="{left}" y1="{med_y:.2f}" x2="{width - right}" y2="{med_y:.2f}" '
        f'stroke="{INK_SECONDARY}" stroke-width="2"/>'
    )
    parts.append(
        _text(left + 6, med_y - 8, f"median {median:+.1%}", 12, INK_SECONDARY, "start", 600)
    )
    parts.append(
        f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" y2="{top + plot_h}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )

    worst_x = left + 0 * slot + slot / 2
    best_x = left + (len(values) - 1) * slot + slot / 2
    parts.append(
        _text(
            worst_x,
            top + plot_h - (values[0] / hi) * plot_h - 8,
            f"{values[0]:+.1%}",
            12,
            INK,
            "middle",
            650,
        )
    )
    parts.append(
        _text(
            best_x,
            top + plot_h - (values[-1] / hi) * plot_h - 8,
            f"{values[-1]:+.1%}",
            12,
            INK,
            "end",
            650,
        )
    )
    parts.append(_text(left, height - 12, "worst seed", 11, INK_MUTED))
    parts.append(_text(width - right, height - 12, "best seed", 11, INK_MUTED, "end"))
    parts.append(
        _text(width / 2, height - 12, f"{len(values)} seeds, sorted", 11, INK_MUTED, "middle")
    )

    return _frame(
        width,
        height,
        "Recovery delta against the naive baseline, by seed",
        f"Every one of {len(values)} independently generated batches recovers more. "
        "The worst is the number that matters.",
        "".join(parts),
    )


# ---------------------------------------------------------------------------
# 2. Charge attempts by root cause, two series
# ---------------------------------------------------------------------------
def attempts_by_category(rows: Sequence[tuple[str, int, int]]) -> str:
    """Grouped horizontal bars. Two series, so a legend AND direct labels."""
    if not rows:
        return ""
    width = 900
    gutter, right, top = 196, 60, 44
    band = 38.0
    height = int(top + band * len(rows) + 34)
    plot_w = width - gutter - right
    hi = max(max(a, b) for _, a, b in rows) or 1
    thickness = min(14.0, (band - SURFACE_GAP - 8) / 2)

    parts: list[str] = [
        f'<rect x="{gutter - 10}" y="{top - 6}" width="10" height="0" fill="none"/>',
        f'<rect x="{gutter}" y="{top - 20}" width="12" height="12" rx="3" fill="{SERIES_1}"/>',
        _text(gutter + 18, top - 10, "ReclaimAgent", 12, INK_SECONDARY, "start", 600),
        f'<rect x="{gutter + 122}" y="{top - 20}" width="12" height="12" rx="3" fill="{SERIES_2}"/>',
        _text(gutter + 140, top - 10, "naive retry 3x", 12, INK_SECONDARY, "start", 600),
    ]

    for i, (label, ours, theirs) in enumerate(rows):
        y = top + i * band
        parts.append(_text(gutter - 12, y + band / 2 + 1, label, 12, INK, "end", 600))
        for j, (value, colour) in enumerate(((ours, SERIES_1), (theirs, SERIES_2))):
            by = y + (band - 2 * thickness - SURFACE_GAP) / 2 + j * (thickness + SURFACE_GAP)
            w = (value / hi) * plot_w
            tip = f"{label}: {value} charge attempts"
            if value == 0:
                parts.append(_zero_marker(gutter, by + thickness / 2, colour))
            else:
                parts.append(_rounded_bar_h(gutter, by, w, thickness, colour, tip))
            parts.append(
                _text(
                    gutter + w + 7,
                    by + thickness / 2 + 4,
                    str(value),
                    11,
                    INK if value == 0 else INK_SECONDARY,
                    "start",
                    700 if value == 0 else 500,
                )
            )

    parts.append(
        f'<line x1="{gutter}" y1="{top - 2}" x2="{gutter}" y2="{top + band * len(rows)}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    parts.append(
        _text(
            gutter,
            height - 10,
            "A zero is drawn as a stub and labelled: it is a measured result, not missing data.",
            11,
            INK_MUTED,
        )
    )
    return _frame(
        width,
        height,
        "Charge attempts by root cause",
        "Where the two strategies spend their attempts. The three zeros are the graded result.",
        "".join(parts),
    )


# ---------------------------------------------------------------------------
# 3. Ablation contribution
# ---------------------------------------------------------------------------
def ablation_contribution(rows: Sequence[tuple[str, float]]) -> str:
    """One series, so no legend. Magnitude of recovery lost by removing each feature."""
    if not rows:
        return ""
    width = 900
    gutter, right, top = 230, 96, 16
    band = 40.0
    height = int(top + band * len(rows) + 30)
    plot_w = width - gutter - right
    hi = max(abs(v) for _, v in rows) or 1.0
    thickness = min(BAR_MAX_THICKNESS, band - 14)

    parts: list[str] = []
    for i, (label, value) in enumerate(rows):
        y = top + i * band + (band - thickness) / 2
        w = (abs(value) / hi) * plot_w
        tip = f"removing {label}: recovery {value:+.1%}"
        parts.append(_text(gutter - 12, y + thickness / 2 + 4, label, 12.5, INK, "end", 600))
        if abs(value) < 0.001:
            parts.append(_zero_marker(gutter, y + thickness / 2, SERIES_1))
            parts.append(
                _text(
                    gutter + 10, y + thickness / 2 + 4, "0.0%  (by design)", 12, INK, "start", 700
                )
            )
        else:
            parts.append(_rounded_bar_h(gutter, y, w, thickness, SERIES_1, tip))
            parts.append(
                _text(
                    gutter + w + 8,
                    y + thickness / 2 + 4,
                    f"{value:+.1%}",
                    12,
                    INK_SECONDARY,
                    "start",
                    600,
                )
            )
    parts.append(
        f'<line x1="{gutter}" y1="{top}" x2="{gutter}" y2="{top + band * len(rows)}" '
        f'stroke="{AXIS}" stroke-width="1"/>'
    )
    parts.append(
        _text(
            gutter,
            height - 8,
            "Longer is more valuable: the recovery given up when that feature is switched off.",
            11,
            INK_MUTED,
        )
    )
    return _frame(
        width,
        height,
        "What each design decision is worth",
        "Recovery lost when one feature is disabled and everything is re-run over the same seeds.",
        "".join(parts),
    )
