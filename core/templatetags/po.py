"""Template helpers shared by every screen.

    {% load po %}
    {% icon "send" %}            inline stroke SVG, 18 px (files in core/templates/icons/)
    {% icon "people" 22 1.8 %}   size and stroke width
    {% app_icon 48 %}            the Post Office sealed-envelope mark
    {% building_logo building 40 %}
    {{ list.name|tr:"uk" }}      read a translatable JSON value
    {{ value|pct }}              0.482 -> 48.2%
    {% qs page=3 %}              current query string with changes
    {% chart_data spec %}        JSON for static/js/charts.js (interactive charts)
"""
import json
from functools import lru_cache
from pathlib import Path

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from core.models import tr as _tr

register = template.Library()
_ICON_DIR = Path(__file__).resolve().parent.parent / "templates" / "icons"


@lru_cache(maxsize=256)
def _icon_body(name):
    path = _ICON_DIR / f"{name}.svg"
    return path.read_text(encoding="utf-8").strip() if path.exists() else '<circle cx="12" cy="12" r="8"></circle>'


@register.simple_tag
def icon(name, size=18, stroke=2, label=""):
    a11y = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return mark_safe(
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round" {a11y}>{_icon_body(name)}</svg>'
    )


APP_ICON = (
    '<svg viewBox="0 0 64 64" width="{s}" height="{s}" role="img" aria-label="Post Office">'
    '<rect width="64" height="64" rx="17" fill="#0B7A85"></rect>'
    '<rect x="10" y="16" width="44" height="31" rx="5" fill="#FFD2B5"></rect>'
    '<path d="M12.5 19.5L32 34L51.5 19.5" fill="none" stroke="#0B7A85" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></path>'
    '<circle cx="32" cy="34" r="7.5" fill="#0B7A85"></circle>'
    '<path d="M32 30.2l1.2 2.5 2.7.4-2 1.9.5 2.7-2.4-1.3-2.4 1.3.5-2.7-2-1.9 2.7-.4z" fill="#FFD2B5"></path></svg>'
)

BUILDING_LOGOS = {
    "town_hall": (
        '<rect width="64" height="64" rx="17" fill="#2b3a67"></rect>'
        '<path d="M10.5 27L32 11.5L53.5 27Z" fill="#f6e9d2" stroke="#f6e9d2" stroke-width="3" stroke-linejoin="round"></path>'
        '<rect x="15" y="27" width="34" height="25" rx="2.5" fill="#f6e9d2"></rect>'
        '<circle cx="32" cy="27.5" r="7.2" fill="#2b3a67"></circle>'
        '<path d="M32 23.6V27.5L34.8 29.4" fill="none" stroke="#f6e9d2" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path>'
        '<path d="M27.5 52V43.5A4.5 4.5 0 0 1 36.5 43.5V52Z" fill="#2b3a67"></path>'
    ),
    "pavilion": (
        '<rect width="64" height="64" rx="17" fill="#1f4a3a"></rect>'
        '<circle cx="32" cy="19.5" r="3.2" fill="#f4a93b"></circle>'
        '<path d="M12 42.5A20 18.5 0 0 1 52 42.5Z" fill="#f4a93b"></path>'
        '<path d="M19.5 37.5A13.5 12 0 0 1 26.5 29" fill="none" stroke="#1f4a3a" stroke-width="2.6" stroke-linecap="round"></path>'
        '<path d="M9 47.5H55" stroke="#f4a93b" stroke-width="4.5" stroke-linecap="round"></path>'
    ),
    "bell_tower": (
        '<rect width="64" height="64" rx="17" fill="#5a3d7a"></rect>'
        '<path d="M20 42V30a12 12 0 0 1 24 0v12l4 5H16z" fill="#f3d9a4"></path>'
        '<circle cx="32" cy="51" r="3.4" fill="#f3d9a4"></circle>'
        '<path d="M32 13v5" stroke="#f3d9a4" stroke-width="3.4" stroke-linecap="round"></path>'
    ),
    "other": (
        '<rect width="64" height="64" rx="17" fill="#5B6170"></rect>'
        '<path d="M18 48V24l14-9 14 9v24z" fill="#F3F0EB"></path>'
        '<rect x="27" y="36" width="10" height="12" rx="2" fill="#5B6170"></rect>'
    ),
}


@register.simple_tag
def app_icon(size=48):
    return mark_safe(APP_ICON.format(s=size))


@register.simple_tag
def building_logo(building, size=40):
    """A building's logo. Accepts a Building, a kind string or None (Post Office itself)."""
    if building is None or building == "":
        return app_icon(size)
    kind = building if isinstance(building, str) else getattr(building, "kind", "other")
    name = building if isinstance(building, str) else getattr(building, "name", "")
    body = BUILDING_LOGOS.get(kind, BUILDING_LOGOS["other"])
    return mark_safe(
        f'<svg viewBox="0 0 64 64" width="{size}" height="{size}" role="img" aria-label="{escape(name)}">{body}</svg>'
    )


@register.filter(name="tr")
def tr_filter(value, code=None):
    return _tr(value, code) if code else _tr(value, None)


@register.filter
def pct(value, digits=1):
    try:
        return f"{float(value) * 100:.{int(digits)}f}%"
    except (TypeError, ValueError):
        return "–"


@register.filter
def get(mapping, key):
    """{{ mydict|get:key }}"""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.simple_tag(takes_context=True)
def qs(context, **changes):
    """The current query string with some parameters changed (empty value removes one)."""
    params = context["request"].GET.copy()
    for key, value in changes.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else "?"


# Keep "</script>" and friends from ending the block early (campaign names are staff-written).
_JSON_ESCAPES = {ord(">"): chr(92) + "u003E", ord("<"): chr(92) + "u003C", ord("&"): chr(92) + "u0026"}


@register.simple_tag
def chart_data(spec):
    """The description static/js/charts.js reads: {"group": ..., "charts": [...]}."""
    from django.core.serializers.json import DjangoJSONEncoder

    body = json.dumps(spec, cls=DjangoJSONEncoder).translate(_JSON_ESCAPES)
    return mark_safe(f'<script type="application/json" data-po-charts>{body}</script>')
