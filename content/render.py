"""Turning content into what is actually sent.

One function feeds the editor canvas, test sends and real sends, so they cannot
drift apart (spec section 9):

    render_email(content, context, message_type=..., ...) -> RenderedEmail
    render_notification(content, context, ...)            -> RenderedNotification

`context` is a plain dict. Visual blocks use {name} and {dotted.name}
placeholders; HTML mode uses sandboxed Jinja ({{ person.first_name }},
{% for item in order.items %}). Build the context with build_context().
"""
import html as html_lib
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext as _
from jinja2 import Undefined, nodes
from jinja2.sandbox import SandboxedEnvironment

from core.models import Language, Organization

EMAIL_WIDTH = 600
ACCENT = "#0B7A85"
INK = "#1B1F2A"
MUTED = "#5B6170"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")
CAMPAIGN_PLACEHOLDERS = {
    "first_name": "first name",
    "last_name": "last name",
    "preferences_link": "preferences link",
}
BLOCK_TYPES = ["heading", "text", "image", "button", "card", "divider", "repeat"]


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str
    preview_text: str = ""
    warnings: list = field(default_factory=list)


@dataclass
class RenderedNotification:
    text: str  # as written, placeholders filled (*bold*, _italic_, [label](url))
    telegram_html: str  # for Telegram's HTML parse mode
    push_body: str  # marks stripped, one line, at most 150 characters
    button_label: str = ""
    button_url: str = ""
    image_url: str = ""
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- context

def build_context(person=None, data=None, links=None):
    """The values placeholders can use.

    person: a Person (or None for previews)
    data:   what a building sent with an app message ({"order": {...}})
    links:  {"preferences": url, "unsubscribe": url, "telegram": url}
    """
    links = links or {}
    ctx = dict(data or {})
    p = {
        "id": person.public_id if person and person.pk else "",
        "first_name": person.first_name if person else "",
        "last_name": person.last_name if person else "",
        "email": (person.email or "") if person else "",
        "language": person.language_code if person else Language.default_code(),
    }
    ctx.setdefault("person", p)
    ctx.setdefault("first_name", p["first_name"])
    ctx.setdefault("last_name", p["last_name"])
    ctx.setdefault("preferences_link", links.get("preferences", ""))
    ctx["_links"] = links
    return ctx


def sample_context(variables, links=None):
    """A context filled with a template's sample values ("Sample order" preview)."""
    data = {}
    for path, spec in (variables or {}).items():
        spec = spec or {}
        if "list" in spec:
            fields = list((spec.get("list") or {}).keys())
            value = spec.get("sample") or [{f: f"{f} {n}" for f in fields} for n in (1, 2)]
        else:
            value = spec.get("sample", "")
        _set_path(data, path, value)
    ctx = build_context(None, data, links or {"preferences": "#", "unsubscribe": "#"})
    ctx.update({"first_name": ctx.get("first_name") or "Olena", "last_name": ctx.get("last_name") or "Kovalenko"})
    ctx["person"].update({"first_name": ctx["first_name"], "last_name": ctx["last_name"]})
    return ctx


def _set_path(data, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
        if not isinstance(data, dict):
            return
    data[parts[-1]] = value


def lookup(context, path):
    """Dotted lookup; returns (value, found)."""
    value = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return "", False
    return value, True


# --------------------------------------------------------------------------- placeholders and inline marks

def fill_placeholders(text, context, warnings=None, labels=None, mode="fill"):
    """Replace {name} placeholders in plain text.

    mode "fill": the value; an empty value renders as nothing and swallows one
                 adjacent space or comma.
    mode "tags": a marker the HTML step turns into an orange tag with the label.
    """
    if not text:
        return ""

    out = []
    pos = 0
    for m in PLACEHOLDER_RE.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        name = m.group(1)
        if mode == "tags":
            label = (labels or {}).get(name) or name.split(".")[-1].replace("_", " ")
            out.append(f"\x00{label}\x01")
            continue
        value, found = lookup(context, name)
        if not found and warnings is not None:
            warnings.append(f"Unknown variable {name}")
        value = "" if value is None or isinstance(value, (dict, list)) else str(value)
        if value == "":
            # Swallow one adjacent space or comma: "Hi {first_name}," -> "Hi,"
            if out and out[-1].endswith(" "):
                out[-1] = out[-1][:-1]
            elif text[pos:pos + 1] in (" ", ","):
                pos += 1
        out.append(value)
    out.append(text[pos:])
    return "".join(out)


_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_TAG_RE = re.compile("\x00([^\x01]*)\x01")
_SAFE_SCHEMES = ("http://", "https://", "mailto:", "tel:", "#", "/")


def safe_url(url):
    url = (url or "").strip()
    return url if url.lower().startswith(_SAFE_SCHEMES) else ""


def inline_html(text, track=None, link_style="", tag_html=None):
    """Escape text and apply *bold*, _italic_ and [label](url)."""
    links = []

    def stash(m):
        links.append((m.group(1), m.group(2)))
        return f"\x02{len(links) - 1}\x03"

    text = _LINK_RE.sub(stash, text or "")
    out = html_lib.escape(text, quote=False)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)

    def unstash(m):
        label, url = links[int(m.group(1))]
        url = safe_url(url)
        if not url:
            return html_lib.escape(label)
        if track:
            url = track(url)
        return f'<a href="{html_lib.escape(url)}"{link_style}>{html_lib.escape(label)}</a>'

    out = re.sub("\x02(\\d+)\x03", unstash, out)
    tag = tag_html or (lambda label: f'<span class="po-var" style="display:inline-block;padding:1px 8px;border-radius:8px;background:#FFEADB;color:#94461A;font-weight:600;font-size:.92em;white-space:nowrap">{html_lib.escape(label)}</span>')
    return _TAG_RE.sub(lambda m: tag(m.group(1)), out)


def strip_marks(text):
    """Plain text: marks removed, links shown as "label (url)"."""
    text = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text or "")
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    return _TAG_RE.sub(lambda m: m.group(1), text)


# --------------------------------------------------------------------------- blocks -> email HTML

def _paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text.strip())] if text and text.strip() else []


def render_block(block, context, *, warnings, labels=None, mode="fill", track=None):
    """One block as email-safe HTML (table rows are added by the caller)."""
    kind = block.get("type")
    link_style = f' style="color:{ACCENT}"'

    def fill(value):
        return fill_placeholders(value or "", context, warnings, labels, mode)

    def rich(value):
        return inline_html(fill(value), track, link_style).replace("\n", "<br>")

    if kind == "heading":
        align = block.get("align") if block.get("align") in ("left", "center", "right") else "left"
        return (f'<h1 style="margin:0;font-family:{FONT};font-size:26px;line-height:1.25;font-weight:700;'
                f'color:{INK};text-align:{align}">{rich(block.get("text"))}</h1>')
    if kind == "text":
        paras = _paragraphs(fill(block.get("text")))
        return "".join(
            f'<p style="margin:0 0 12px;font-family:{FONT};font-size:16px;line-height:1.55;color:{INK}">'
            f'{inline_html(p, track, link_style).replace(chr(10), "<br>")}</p>' for p in paras
        ) or ""
    if kind == "image":
        src = block.get("src") or ""
        if not src:
            return ""
        if src.startswith("/"):
            src = settings.PUBLIC_URL + src
        img = (f'<img src="{html_lib.escape(src)}" alt="{html_lib.escape(block.get("alt") or "")}" width="536" '
               f'style="display:block;width:100%;max-width:536px;height:auto;border:0;border-radius:12px">')
        href = safe_url(fill(block.get("href") or ""))
        if href:
            href = track(href) if track else href
            return f'<a href="{html_lib.escape(href)}">{img}</a>'
        return img
    if kind == "button":
        href = safe_url(fill(block.get("href") or "")) or "#"
        if track and href != "#":
            href = track(href)
        return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td style="border-radius:14px;background:{ACCENT}">'
                f'<a href="{html_lib.escape(href)}" style="display:inline-block;padding:13px 24px;font-family:{FONT};'
                f'font-size:16px;font-weight:600;color:#FFFFFF;text-decoration:none;border-radius:14px">'
                f'{rich(block.get("text"))}</a></td></tr></table>')
    if kind == "card":
        return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td style="background:#DDF0F1;border-radius:14px;padding:16px 18px;font-family:{FONT}">'
                f'<div style="font-size:17px;font-weight:700;color:{INK};margin:0 0 4px">{rich(block.get("title"))}</div>'
                f'<div style="font-size:15px;line-height:1.5;color:{INK}">{rich(block.get("text"))}</div></td></tr></table>')
    if kind == "divider":
        return '<div style="height:1px;line-height:1px;font-size:1px;background:#E4DFD6">&nbsp;</div>'
    if kind == "repeat":
        return _render_repeat(block, context, warnings=warnings, mode=mode, labels=labels)
    return ""


def _render_repeat(block, context, *, warnings, mode, labels):
    source = block.get("source") or ""
    show = block.get("show") or []
    if mode == "tags":
        label = (labels or {}).get(source) or source
        items = [{f: f"\x00{f.replace('_', ' ')}\x01" for f in show}]
        note = f'<div style="font-family:{FONT};font-size:12px;color:{MUTED};padding-bottom:6px">One row for each of: {html_lib.escape(label)}</div>'
    else:
        items, found = lookup(context, source)
        if not found or not isinstance(items, list):
            warnings.append(f"Unknown list {source}")
            items = []
        note = ""
    rows = []
    for item in items:
        cells = []
        for i, name in enumerate(show):
            value = item.get(name, "") if isinstance(item, dict) else ""
            value = "" if value is None else str(value)
            align = "right" if i == len(show) - 1 and len(show) > 1 else "left"
            weight = "600" if i == 0 else "400"
            cells.append(f'<td style="padding:10px 0;border-bottom:1px solid #F0ECE5;font-family:{FONT};font-size:15px;'
                         f'color:{INK};text-align:{align};font-weight:{weight}">{inline_html(value)}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return note + ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                   + "".join(rows) + "</table>")


def render_blocks(blocks, context, *, warnings=None, labels=None, mode="fill", track=None, wrap_ids=False):
    """Blocks as table rows. wrap_ids adds data-block-id for the editor canvas."""
    warnings = warnings if warnings is not None else []
    rows = []
    for block in blocks or []:
        inner = render_block(block, context, warnings=warnings, labels=labels, mode=mode, track=track)
        if not inner and not wrap_ids:
            continue
        attr = f' data-block-id="{html_lib.escape(str(block.get("id", "")))}"' if wrap_ids else ""
        rows.append(f'<tr><td{attr} style="padding:10px 32px">{inner}</td></tr>')
    return "".join(rows)


def blocks_to_text(blocks, context, warnings=None):
    """The plain-text part of an email."""
    out = []
    for block in blocks or []:
        kind = block.get("type")
        fill = lambda v: strip_marks(fill_placeholders(v or "", context, warnings))  # noqa: E731
        if kind == "heading":
            out.append(fill(block.get("text")).upper())
        elif kind == "text":
            out.append(fill(block.get("text")))
        elif kind == "image" and block.get("alt"):
            out.append(f"[{block['alt']}]")
        elif kind == "button":
            out.append(f"{fill(block.get('text'))}: {fill(block.get('href'))}")
        elif kind == "card":
            out.append(f"{fill(block.get('title'))}\n{fill(block.get('text'))}")
        elif kind == "divider":
            out.append("-" * 30)
        elif kind == "repeat":
            items, found = lookup(context, block.get("source") or "")
            for item in items if found and isinstance(items, list) else []:
                out.append("  ".join(str(item.get(f, "")) for f in block.get("show") or [] if isinstance(item, dict)))
    return "\n\n".join(p for p in out if p.strip())


# --------------------------------------------------------------------------- HTML mode (sandboxed Jinja)

class _Quiet(Undefined):
    """An unknown variable renders as empty and is noted as a warning."""

    __slots__ = ()

    def _note(self):
        _missing.append(self._undefined_name or "?")

    def __str__(self):
        self._note()
        return ""

    def __iter__(self):
        self._note()
        return iter(())

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self

    __getitem__ = __getattr__


_missing = []
_ALLOWED_NODES = (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name, nodes.Getattr, nodes.Getitem,
                  nodes.Const, nodes.For)


class TemplateNotAllowed(ValueError):
    pass


class _Env(SandboxedEnvironment):
    """Dotted names read dictionary keys first, so {{ order.items }} is the data, not dict.items."""

    def getattr(self, obj, attribute):
        if isinstance(obj, dict):
            return obj[attribute] if attribute in obj else self.undefined(obj=obj, name=attribute)
        return super().getattr(obj, attribute)


def _jinja_env():
    return _Env(autoescape=True, undefined=_Quiet)


def validate_jinja(source):
    """Only variable output and for loops are allowed. Raises TemplateNotAllowed."""
    env = _jinja_env()
    try:
        tree = env.parse(source or "")
    except Exception as exc:  # jinja2.TemplateSyntaxError
        raise TemplateNotAllowed(f"The template has a mistake: {exc}") from exc
    for node in tree.find_all(nodes.Node):
        if not isinstance(node, _ALLOWED_NODES):
            raise TemplateNotAllowed(
                f"Only {{{{ variables }}}} and {{% for %}} loops can be used here (found {type(node).__name__.lower()})."
            )
        if isinstance(node, nodes.For) and (node.else_ or node.test or node.recursive):
            raise TemplateNotAllowed("Loops cannot use else, if or recursive here.")
        if isinstance(node, nodes.Getattr) and node.attr.startswith("_"):
            raise TemplateNotAllowed("Names that start with an underscore cannot be used.")
    return True


def render_jinja(source, context, warnings=None):
    validate_jinja(source)
    del _missing[:]
    safe_context = {k: v for k, v in context.items() if not k.startswith("_")}
    out = _jinja_env().from_string(source or "").render(**safe_context)
    if warnings is not None:
        warnings.extend(f"Unknown variable {name}" for name in dict.fromkeys(_missing))
    del _missing[:]
    return out


def placeholders_to_jinja(text):
    """{order.number} -> {{ order.number }}; campaign shorthands map to person.*"""
    def repl(m):
        name = m.group(1)
        if name in ("first_name", "last_name"):
            name = f"person.{name}"
        return "{{ " + name + " }}"

    return PLACEHOLDER_RE.sub(repl, text or "")


def blocks_to_editable_html(blocks):
    """What HTML mode starts from: the blocks' HTML with Jinja placeholders."""
    marker_ctx = {}
    rows = render_blocks(
        [b for b in blocks or [] if b.get("type") != "repeat"], marker_ctx, mode="tags",
        labels=_IdentityLabels(),
    )
    rows = _TAG_RE.sub(lambda m: m.group(1), rows)
    rows = re.sub(r'<span class="po-var"[^>]*>(.*?)</span>', lambda m: placeholders_to_jinja("{" + m.group(1) + "}"), rows)
    return rows.replace("</tr><tr>", "</tr>\n<tr>")


class _IdentityLabels(dict):
    """labels mapping that returns the variable name itself."""

    def get(self, key, default=None):
        return key

    def __bool__(self):
        # An empty dict is falsy, and fill_placeholders would then ignore these labels.
        return True


# --------------------------------------------------------------------------- the whole email

def _footer_html(org, message_type, links, lang, reason=None, list_name=None):
    with translation.override(lang if lang in settings.SHIPPED_TRANSLATIONS else "en"):
        if reason is None:
            if message_type == "newsletter":
                reason = (_("You are getting this because you signed up for %(list)s.") % {"list": list_name}
                          if list_name else _("You are getting this because you signed up for news from us."))
            else:
                reason = _("You are getting this because of something you did with us, such as an order or a shift.")
        parts = [f'<div style="font-weight:600;color:{INK}">{html_lib.escape(org.name)}</div>']
        if org.mailing_address:
            parts.append(f"<div>{html_lib.escape(org.mailing_address)}</div>")
        parts.append(f'<div style="margin-top:8px">{html_lib.escape(reason)}</div>')
        # Plain calls (not inside f-strings) so makemessages can find them.
        word_unsub, word_prefs, word_tg = _("Unsubscribe"), _("Manage preferences"), _("Get this on Telegram")
        link_bits = []
        style = f'style="color:{ACCENT};font-weight:600"'
        if message_type == "newsletter":
            link_bits.append(f'<a href="{html_lib.escape(links.get("unsubscribe") or "#")}" {style}>{word_unsub}</a>')
        if links.get("preferences"):
            link_bits.append(f'<a href="{html_lib.escape(links["preferences"])}" {style}>{word_prefs}</a>')
        if links.get("telegram"):
            link_bits.append(f'<a href="{html_lib.escape(links["telegram"])}" {style}>{word_tg}</a>')
        if link_bits:
            parts.append(f'<div style="margin-top:8px">{" &nbsp;·&nbsp; ".join(link_bits)}</div>')
    return (f'<tr><td data-footer style="padding:22px 32px 28px;border-top:1px solid #F0ECE5;font-family:{FONT};'
            f'font-size:13px;line-height:1.5;color:{MUTED}">{"".join(parts)}</td></tr>')


def _footer_text(org, message_type, links, lang):
    with translation.override(lang if lang in settings.SHIPPED_TRANSLATIONS else "en"):
        word_unsub, word_prefs = _("Unsubscribe"), _("Manage preferences")
        lines = [org.name]
        if org.mailing_address:
            lines.append(org.mailing_address)
        if message_type == "newsletter" and links.get("unsubscribe"):
            lines.append(f'{word_unsub}: {links["unsubscribe"]}')
        if links.get("preferences"):
            lines.append(f'{word_prefs}: {links["preferences"]}')
    return "\n".join(lines)


def wrap_email(body_rows, *, org, lang, preview_text="", footer_row="", pixel_url="", header=True):
    """The 600 px centred table every email shares."""
    pre = (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">'
           f'{html_lib.escape(preview_text)}</div>') if preview_text else ""
    head = (f'<tr><td data-header style="padding:26px 32px 8px;font-family:{FONT};font-size:15px;font-weight:700;'
            f'letter-spacing:.02em;color:{ACCENT}">{html_lib.escape(org.name)}</td></tr>') if header else ""
    pixel = f'<img src="{html_lib.escape(pixel_url)}" width="1" height="1" alt="" style="display:block;border:0">' if pixel_url else ""
    return (
        f'<!doctype html><html lang="{html_lib.escape(lang)}"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:#F3F0EB">{pre}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#F3F0EB">'
        f'<tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="{EMAIL_WIDTH}" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:{EMAIL_WIDTH}px;background:#FFFFFF;border-radius:20px">'
        f'{head}{body_rows}<tr><td style="height:14px;line-height:14px;font-size:14px">&nbsp;</td></tr>{footer_row}'
        f'</table>{pixel}</td></tr></table></body></html>'
    )


def render_email(content, context, *, message_type="newsletter", org=None, labels=None, mode="fill",
                 track=None, pixel_url="", list_name=None, wrap_ids=False):
    """Render an email Content.

    context       from build_context() or sample_context()
    message_type  "newsletter" (unsubscribe footer) or "service" (reason line only)
    labels        {variable: label} for mode "tags" (the editor canvas)
    mode          "fill" for real sends and sample previews, "tags" to show orange tags
    track         optional function(url) -> tracked url, for newsletter links
    pixel_url     optional open-tracking pixel
    wrap_ids      add data-block-id to each block's cell (editor canvas)
    """
    org = org or Organization.get()
    lang = content.language
    warnings = []
    links = context.get("_links") or {}
    subject = fill_placeholders(content.subject, context, warnings, labels, "fill" if mode == "fill" else "tags")
    subject = _TAG_RE.sub(lambda m: "{" + m.group(1) + "}", subject)
    preview = _TAG_RE.sub(lambda m: m.group(1), fill_placeholders(content.preview_text, context, None, labels, mode))
    footer = _footer_html(org, message_type, links, lang, list_name=list_name)
    if content.mode == "html":
        try:
            body = render_jinja(content.html, context, warnings)
        except TemplateNotAllowed as exc:
            warnings.append(str(exc))
            body = ""
        if track:
            body = re.sub(r'href="(https?://[^"]+)"', lambda m: f'href="{html_lib.escape(track(html_lib.unescape(m.group(1))))}"', body)
        stripped = body.lstrip().lower()
        if stripped.startswith("<tr"):
            rows = body
        else:
            rows = f'<tr><td style="padding:10px 32px;font-family:{FONT};font-size:16px;line-height:1.55;color:{INK}">{body}</td></tr>'
        text = re.sub(r"\n{3,}", "\n\n", html_lib.unescape(re.sub(r"<[^>]+>", "", re.sub(r"</(p|div|tr|h\d)>|<br\s*/?>", "\n", body)))).strip()
    else:
        rows = render_blocks(content.blocks, context, warnings=warnings, labels=labels, mode=mode, track=track, wrap_ids=wrap_ids)
        text = blocks_to_text(content.blocks, context)
        if wrap_ids:
            # The editor canvas sorts the rows of this body; real sends never set wrap_ids.
            rows = f"<tbody data-blocks>{rows}</tbody>"
    html = wrap_email(rows, org=org, lang=lang, preview_text=preview, footer_row=footer, pixel_url=pixel_url)
    text = (text + "\n\n--\n" + _footer_text(org, message_type, links, lang)).strip()
    return RenderedEmail(subject=subject.strip(), html=html, text=text, preview_text=preview, warnings=warnings)


# --------------------------------------------------------------------------- notifications

TELEGRAM_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
PUSH_LIMIT = 150


def telegram_html(text):
    """*bold*, _italic_, [label](url) as Telegram HTML."""
    out = inline_html(text, tag_html=lambda label: html_lib.escape(label))
    return out.replace("<strong>", "<b>").replace("</strong>", "</b>").replace("<em>", "<i>").replace("</em>", "</i>")


def push_text(text):
    plain = _LINK_RE.sub(lambda m: m.group(1), text or "")
    plain = _BOLD_RE.sub(r"\1", plain)
    plain = _ITALIC_RE.sub(r"\1", plain)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain if len(plain) <= PUSH_LIMIT else plain[:PUSH_LIMIT - 1].rstrip() + "…"


def render_notification(content, context, *, track=None):
    """Render a notification Content for Telegram and push."""
    warnings = []
    text = fill_placeholders(content.text, context, warnings)
    button_url = safe_url(fill_placeholders(content.button_url, context, warnings))
    image_url = ""
    if content.image:
        try:
            image_url = content.image.url
            if image_url.startswith("/"):
                image_url = settings.PUBLIC_URL + image_url
        except ValueError:
            image_url = ""
    return RenderedNotification(
        text=text,
        telegram_html=telegram_html(text),
        push_body=push_text(text),
        button_label=fill_placeholders(content.button_label, context, warnings),
        button_url=track(button_url) if (track and button_url) else button_url,
        image_url=image_url,
        warnings=warnings,
    )
