"""What other areas call to work with templates and content.

    register_templates(building, definitions) -> {"created": n, "updated": n, "kept": n}
    reset_to_default(template)
    copy_content(source_owner, target_owner)
    save_as_campaign_template(campaign, name) -> Template

The editor's own helpers (clean_blocks, language_states, copy_language) live here too.
"""
import secrets

from django.db import transaction
from django.utils.text import slugify

from core.models import Language
from people.models import Person

from .models import Content, Template, get_content, owner_filter
from .render import BLOCK_TYPES

# The fields each block type keeps (spec section 9). Anything else is dropped on save.
BLOCK_FIELDS = {
    "heading": ("text", "align"),
    "text": ("text",),
    "image": ("src", "alt", "href"),
    "button": ("text", "href"),
    "card": ("title", "text"),
    "divider": (),
    "repeat": ("source", "show"),
}
ALIGNMENTS = ("left", "center", "right")
COPY_FIELDS = ("subject", "preview_text", "mode", "blocks", "html", "text", "button_label", "button_url", "image")


def new_block_id():
    return "b" + secrets.token_hex(4)


def clean_blocks(blocks, allow_repeat=False):
    """Blocks as they are stored: known types and fields only, every block with its own id."""
    out, seen = [], set()
    for raw in blocks if isinstance(blocks, list) else []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind not in BLOCK_TYPES or (kind == "repeat" and not allow_repeat):
            continue
        block_id = str(raw.get("id") or "")[:40]
        if not block_id or block_id in seen:
            block_id = new_block_id()
        seen.add(block_id)
        block = {"id": block_id, "type": kind}
        for name in BLOCK_FIELDS[kind]:
            value = raw.get(name)
            if name == "show":
                block[name] = [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []
            elif name == "align":
                block[name] = value if value in ALIGNMENTS else "left"
            elif name == "href" and kind == "image":
                block[name] = str(value).strip() if value else None
            else:
                block[name] = "" if value is None else str(value)
        out.append(block)
    return out


def block_problems(blocks):
    """Things staff must fix before sending, in plain words, keyed by block id."""
    problems = {}
    for block in blocks or []:
        if block.get("type") == "image" and block.get("src") and not (block.get("alt") or "").strip():
            problems[block["id"]] = "alt"
    return problems


# --------------------------------------------------------------------------- languages

def language_states(owner, form):
    """One entry per organization language for the editor's tabs.

    [{"code", "name", "english_name", "is_default", "written", "readers"}]
    """
    written = {c.language for c in Content.objects.filter(form=form, **owner_filter(owner)) if c.is_written}
    readers = {}
    for code in Person.objects.values_list("language__code", flat=True):
        readers[code] = readers.get(code, 0) + 1
    return [
        {
            "code": lang.code,
            "name": lang.name,
            "english_name": lang.english_name or lang.name,
            "is_default": lang.is_default,
            "written": lang.code in written,
            "readers": readers.get(lang.code, 0),
        }
        for lang in Language.objects.all()
    ]


def copy_language(owner, target_code, source_code=None, forms=("email", "notification")):
    """"Copy from English": fill a language from the default one, form by form.

    A form that is already written in the target language is left alone.
    """
    source_code = source_code or Language.default_code()
    copied = []
    if source_code == target_code:
        return copied
    for form in forms:
        source = get_content(owner, source_code, form)
        if source is None:
            continue
        target = get_content(owner, target_code, form, create=True)
        if target.is_written:
            continue
        _copy_fields(source, target)
        copied.append(form)
    return copied


def _copy_fields(source, target):
    for name in COPY_FIELDS:
        value = getattr(source, name)
        if name == "blocks":
            value = [dict(b) for b in value or []]
        setattr(target, name, value)
    target.save()


def copy_content(source_owner, target_owner):
    """Copy every Content row of one owner to another (template -> campaign and back)."""
    kw = owner_filter(target_owner)
    copied = []
    with transaction.atomic():
        for source in Content.objects.filter(**owner_filter(source_owner)):
            target = Content.objects.filter(language=source.language, form=source.form, **kw).first()
            if target is None:
                target = Content(language=source.language, form=source.form, **kw)
            _copy_fields(source, target)
            copied.append(target)
    return copied


# --------------------------------------------------------------------------- app message templates

def _apply_default(template):
    """Make the live Content rows match the building's own version."""
    keep = []
    for code, forms in (template.default_content or {}).items():
        if not isinstance(forms, dict):
            continue
        email = forms.get("email")
        if isinstance(email, dict):
            content = get_content(template, code, "email", create=True)
            content.subject = str(email.get("subject") or "")
            content.preview_text = str(email.get("preview_text") or "")
            blocks = clean_blocks(email.get("blocks"), allow_repeat=True)
            html = str(email.get("html") or "")
            content.mode = "html" if html.strip() and not blocks else "visual"
            content.blocks = blocks
            content.html = html if content.mode == "html" else ""
            content.save()
            keep.append(content.pk)
        note = forms.get("notification")
        if isinstance(note, dict):
            content = get_content(template, code, "notification", create=True)
            content.text = str(note.get("text") or "")
            content.button_label = str(note.get("button_label") or "")
            content.button_url = str(note.get("button_url") or "")
            content.image = None
            content.save()
            keep.append(content.pk)
    template.contents.exclude(pk__in=keep).delete()


def register_templates(building, definitions):
    """The API's PUT /templates (spec section 10).

    Creates missing templates. Always updates a template's name, variables and
    stored defaults. Rewrites the live content only when staff have not
    customised the template; a customised template is never overwritten.
    """
    result = {"created": 0, "updated": 0, "kept": 0}
    with transaction.atomic():
        for definition in definitions or []:
            key = str(definition.get("key") or "").strip()
            if not key:
                continue
            fields = {
                "name": str(definition.get("name") or key)[:160],
                "fires": str(definition.get("fires") or "")[:240],
                "variables": definition.get("variables") or {},
                "default_content": definition.get("content") or {},
            }
            template = Template.objects.filter(key=key).first()
            if template is None:
                template = Template.objects.create(key=key, kind="app", building=building, message_type="service", **fields)
                _apply_default(template)
                result["created"] += 1
                continue
            if template.kind != "app" or template.building_id != getattr(building, "pk", None):
                # The key belongs to something else; leave it alone.
                result["kept"] += 1
                continue
            for name, value in fields.items():
                setattr(template, name, value)
            template.save()
            if template.customised:
                result["kept"] += 1
            else:
                _apply_default(template)
                result["updated"] += 1
    return result


def reset_to_default(template):
    """"Reset to default": restore the building's version and clear the customised mark."""
    if template.kind != "app":
        return template
    with transaction.atomic():
        _apply_default(template)
        template.customised = False
        template.save(update_fields=["customised", "updated_at"])
    return template


# --------------------------------------------------------------------------- campaign templates

def unique_template_key(name):
    base = "campaign." + (slugify(name) or "template")
    key, n = base[:110], 2
    while Template.objects.filter(key=key).exists():
        key = f"{base[:110]}-{n}"
        n += 1
    return key


def create_campaign_template(name):
    name = (name or "").strip()[:160] or "Untitled template"
    return Template.objects.create(key=unique_template_key(name), kind="campaign", name=name, message_type="newsletter")


def save_as_campaign_template(campaign, name):
    """Keep a campaign's design as a starting point for later campaigns."""
    with transaction.atomic():
        template = create_campaign_template(name or campaign.name)
        copy_content(campaign, template)
    return template
