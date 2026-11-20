"""Charts as inline SVG, drawn on the server. No chart library (spec section 13).

Every chart uses a 760-unit-wide viewBox stretched to the panel width
(preserveAspectRatio="none"), so charts stacked in one panel share one time axis.

    nice_max(values)                 a round axis ceiling just above the largest value
    line_path(values, w, h, vmax)    (line path, area path, points)
    bar_rects(values, w, h, vmax)    one rectangle per value
    area_chart / bar_chart           whole <svg> elements, safe to print in a template
    sparkline(values)                the small trend line on a number card
    peak(values, h, vmax)            where the busiest point sits, in percent of the chart box
"""
import math
from html import escape

from django.utils.safestring import mark_safe

WIDTH = 760
TEAL = "#0B7A85"
TEAL_SOFT = "#4FA7B0"
PEACH = "#EE8A5A"
GRID = "#ECE8E1"
BASELINE = "#D9D4CB"


def nice_max(values, floor=4):
    """A round number a little above the largest value: 1, 2, 2.5, 5 or 10 times a power of ten."""
    top = max([v for v in values if v is not None] or [0])
    if top <= 0:
        return floor
    target = top * 1.08
    power = 10 ** math.floor(math.log10(target))
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * power >= target:
            return max(floor, math.ceil(step * power))
    return max(floor, math.ceil(10 * power))


def axis_labels(vmax, parts):
    """Labels from the top of the axis down to zero, for a chart with `parts` bands."""
    return [_fmt(vmax * (parts - i) / parts) for i in range(parts)] + ["0"]


def _fmt(value):
    return f"{round(value):,}"


def _n(value):
    return f"{value:.1f}"


def points(values, width=WIDTH, height=190, vmax=None):
    """Chart coordinates for a line: x spread evenly, y measured from the top."""
    vmax = vmax or nice_max(values)
    n = len(values)
    out = []
    for i, v in enumerate(values):
        x = width / 2 if n == 1 else (i / (n - 1)) * width
        y = height - 2 - (max(v, 0) / vmax) * (height - 8)
        out.append((x, y))
    return out


def line_path(values, width=WIDTH, height=190, vmax=None):
    """Return (line, area, points). Empty input gives empty paths."""
    if not values:
        return "", "", []
    pts = points(values, width, height, vmax)
    if len(pts) == 1:  # a single value still draws as a flat line across the chart
        pts = [(0, pts[0][1]), (width, pts[0][1])]
    line = " ".join(f"{'L' if i else 'M'}{_n(x)} {_n(y)}" for i, (x, y) in enumerate(pts))
    area = f"{line} L{width} {height} L0 {height} Z"
    return line, area, pts


def bar_rects(values, width=WIDTH, height=110, vmax=None, min_height=2):
    """One rectangle per value. A zero still gets a sliver so the axis reads as continuous."""
    if not values:
        return []
    vmax = vmax or nice_max(values)
    slot = width / len(values)
    bar = max(6, slot * 0.62) if slot >= 8 else slot * 0.8
    rects = []
    for i, v in enumerate(values):
        h = max(min_height, (max(v, 0) / vmax) * (height - 6))
        rects.append({"x": _n(i * slot + (slot - bar) / 2), "y": _n(height - h), "w": _n(bar), "h": _n(h), "value": v})
    return rects


def peak(values, width=WIDTH, height=190, vmax=None):
    """The busiest point: its index, value and position in percent of the chart box.

    Returns None when every value is zero, so an empty chart shows no label.
    """
    if not values or max(values) <= 0:
        return None
    index = max(range(len(values)), key=lambda i: values[i])
    pts = points(values, width, height, vmax)
    x, y = pts[index]
    px, py = x / width * 100, y / height * 100
    return {
        "index": index,
        "value": values[index],
        "x": round(px, 2),
        "y": round(py, 2),
        "label_x": round(min(86, max(14, px)), 2),  # for a label centred above the point, kept inside the panel
        "side": "right" if px < 60 else "left",  # for a label beside the point
    }


def _grid(height, parts):
    lines = []
    for i in range(parts):
        y = 1 if i == 0 else round(height * i / parts)
        lines.append(f'<line x1="0" y1="{y}" x2="{WIDTH}" y2="{y}" stroke="{GRID}" stroke-width="1" vector-effect="non-scaling-stroke"></line>')
    lines.append(f'<line x1="0" y1="{height - 1}" x2="{WIDTH}" y2="{height - 1}" stroke="{BASELINE}" stroke-width="1" vector-effect="non-scaling-stroke"></line>')
    return "".join(lines)


def _svg(height, label, body):
    return mark_safe(
        f'<svg viewBox="0 0 {WIDTH} {height}" preserveAspectRatio="none" width="100%" height="{height}" role="img" '
        f'aria-label="{escape(label)}" style="display:block;overflow:visible">{body}</svg>'
    )


def area_chart(values, *, height=190, vmax=None, parts=3, label="", color=TEAL):
    """A line with a soft fill under it."""
    vmax = vmax or nice_max(values)
    line, area, _ = line_path(values, WIDTH, height, vmax)
    body = _grid(height, parts)
    if line:
        body += (
            f'<path d="{area}" fill="{color}" fill-opacity="0.12"></path>'
            f'<path d="{line}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" '
            f'stroke-linecap="round" vector-effect="non-scaling-stroke"></path>'
        )
    return _svg(height, label, body)


def bar_chart(values, *, height=110, vmax=None, parts=2, label="", color=PEACH, highlight=None, dim=1.0):
    """Bars. `highlight` is the index drawn at full strength when the others are dimmed."""
    vmax = vmax or nice_max(values)
    body = _grid(height, parts)
    for i, r in enumerate(bar_rects(values, WIDTH, height, vmax)):
        opacity = 1 if (highlight is None or i == highlight) else dim
        body += f'<rect x="{r["x"]}" y="{r["y"]}" width="{r["w"]}" height="{r["h"]}" rx="3" fill="{color}" fill-opacity="{opacity}"></rect>'
    return _svg(height, label, body)


def sparkline(values, width=86, height=28, color=TEAL):
    """The small trend line on a number card. Decorative: the number beside it says the same."""
    values = list(values or [])
    if len(values) < 2:
        values = (values or [0]) * 2
    low, high = min(values), max(values)
    span = (high - low) or 1
    pts = []
    for i, v in enumerate(values):
        x = 2 + (i / (len(values) - 1)) * (width - 4)
        y = (height - 4) - ((v - low) / span) * (height - 9) if high != low else height / 2
        pts.append(f"{_n(x)},{_n(y)}")
    return mark_safe(
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2.4" '
        f'stroke-linecap="round" stroke-linejoin="round"></polyline></svg>'
    )
