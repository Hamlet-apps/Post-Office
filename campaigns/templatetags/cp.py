"""Small number helpers for the campaign and dashboard screens.

    {% load cp %}
    {{ 2418|num }}        2,418
    {{ 0.524|pct0 }}      52%
"""
from django import template

register = template.Library()


@register.filter
def num(value):
    """A whole number with thousands separators; a dash when there is none."""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "–"


@register.filter
def pct0(value):
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "–"
