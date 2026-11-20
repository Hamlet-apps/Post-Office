"""The message editor: one component for campaigns and templates (spec sections 9 and 10).

Pages
    campaign_design(request, public_id)   /campaigns/<public_id>/design/  (routed by the campaigns area)
    template_editor(request, key)         /templates/<key>/

Endpoints under /templates/_editor/<owner>/..., where owner is cmp_<id> or tpl_<id>
    editor_save     POST JSON   autosave; answers with the freshly rendered canvas
    editor_canvas   GET         the canvas and previews of what is stored
    editor_upload   POST file   an image for an image block -> {"url": "/media/e/..."}
    editor_image    POST file   the notification's image (or remove=1)
    editor_copy     POST JSON   "Copy from English"

The canvas is always rendered by content.render, the same code that sends.
"""
import json
import os
import secrets

from django.conf import settings
from django.core.files.storage import default_storage
from django.http import Http404, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.urls import NoReverseMatch, reverse
from django.utils.text import get_valid_filename
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from campaigns.models import Campaign
from core.models import Language, Organization
from core.permissions import role_required

from .models import Content, Template, get_content
from .render import (
    CAMPAIGN_PLACEHOLDERS,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_LIMIT,
    TemplateNotAllowed,
    blocks_to_editable_html,
    build_context,
    render_email,
    render_notification,
    sample_context,
    validate_jinja,
)
from .services import block_problems, clean_blocks, copy_language, language_states

FORMS = ("email", "notification")
IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PREVIEW_LINKS = {"preferences": "#", "unsubscribe": "#"}


# --------------------------------------------------------------------------- owners

def resolve_owner(owner_id):
    """cmp_12 -> Campaign, tpl_3 -> Template; anything else is a 404."""
    if isinstance(owner_id, str) and owner_id.startswith("cmp_"):
        return Campaign.get_by_public_id(owner_id)
    if isinstance(owner_id, str) and owner_id.startswith("tpl_"):
        return Template.get_by_public_id(owner_id)
    raise Http404("No such message")


def is_app_template(owner):
    return isinstance(owner, Template) and owner.kind == "app"


def owner_editable(owner):
    return owner.is_editable if isinstance(owner, Campaign) else True


def owner_message_type(owner):
    return owner.message_type if isinstance(owner, Template) else "newsletter"


def owner_variables(owner):
    return (owner.variables or {}) if is_app_template(owner) else {}


def placeholder_labels(owner):
    """{variable: readable label} for the orange tags."""
    labels = {name: _(label) for name, label in CAMPAIGN_PLACEHOLDERS.items()}
    if is_app_template(owner):
        labels = {}
        for name, spec in owner_variables(owner).items():
            labels[name] = (spec or {}).get("label") or name.split(".")[-1].replace("_", " ")
    return labels


def placeholder_list(owner):
    """What the palette and the panel offer to insert."""
    if not is_app_template(owner):
        return [{"name": name, "label": _(label), "sample": "", "list": False, "fields": []}
                for name, label in CAMPAIGN_PLACEHOLDERS.items()]
    out = []
    for name, spec in owner_variables(owner).items():
        spec = spec or {}
        is_list = isinstance(spec.get("list"), dict)
        fields = [{"name": f, "label": (v or {}).get("label") or f.replace("_", " ")}
                  for f, v in (spec.get("list") or {}).items()] if is_list else []
        sample = "" if is_list else str(spec.get("sample", ""))
        out.append({"name": name, "label": spec.get("label") or name, "sample": sample[:60], "list": is_list, "fields": fields})
    return out


def preview_context(owner):
    """Sample values: the building's own for an app message, a sample reader otherwise."""
    return sample_context(owner_variables(owner), PREVIEW_LINKS)


# --------------------------------------------------------------------------- rendering for the page

def _fragment(html):
    """The 600 px email table out of the full document, to place inside the page."""
    start = html.find('<table role="presentation" width="600"')
    last = html.rfind("</table>")
    end = html.rfind("</table>", 0, last)
    if start < 0 or end < 0:
        return html
    fragment = html[start:end + len("</table>")]
    # Uploaded pictures are shown from this host, whatever PUBLIC_URL says.
    return fragment.replace(f'src="{settings.PUBLIC_URL}{settings.MEDIA_URL}', f'src="{settings.MEDIA_URL}')


def render_canvas(owner, content, sample=False):
    """The email as the page shows it, straight from content.render.

    Returns {"canvas", "doc", "warnings"}. Blocks give "canvas", a fragment the page
    places inline and makes sortable. Content written as HTML gives "doc", the whole
    document, which the page shows in a frame that cannot run scripts.
    """
    if content is None:
        return {"canvas": "", "doc": "", "warnings": []}
    labels = placeholder_labels(owner)
    message_type = owner_message_type(owner)
    org = Organization.get()
    if content.mode == "html":
        out = render_email(content, preview_context(owner), message_type=message_type, org=org)
        doc = out.html.replace(f'src="{settings.PUBLIC_URL}{settings.MEDIA_URL}', f'src="{settings.MEDIA_URL}')
        return {"canvas": "", "doc": doc, "warnings": [readable_warning(w) for w in dict.fromkeys(out.warnings)]}
    if sample:
        out = render_email(content, preview_context(owner), message_type=message_type, org=org, mode="fill", wrap_ids=True)
        warnings = list(out.warnings)
    else:
        out = render_email(content, build_context(None, {}, PREVIEW_LINKS), message_type=message_type, org=org,
                           labels=labels, mode="tags", wrap_ids=True)
        warnings = list(out.warnings)
        if is_app_template(owner):
            # Tags never fail, so ask the real renderer which variables the building does not send.
            check = render_email(content, preview_context(owner), message_type=message_type, org=org)
            warnings += check.warnings
    return {"canvas": _fragment(out.html), "doc": "", "warnings": [readable_warning(w) for w in dict.fromkeys(warnings)]}


def readable_warning(text):
    for prefix, wording in (("Unknown variable ", _("%(name)s is not something this message knows. It will be left empty.")),
                            ("Unknown list ", _("%(name)s is not a list this message knows. Nothing will repeat."))):
        if text.startswith(prefix):
            return wording % {"name": text[len(prefix):]}
    return text


def render_note(owner, content, fallback=None):
    """The Telegram bubble and the push notification for a notification Content."""
    shown, used_fallback = content, False
    if (content is None or not content.is_written) and fallback is not None and fallback.is_written:
        shown, used_fallback = fallback, True
    if shown is None:
        return {"telegram_html": "", "push_body": "", "button_label": "", "button_url": "", "image_url": "",
                "fallback": False, "warnings": []}
    out = render_notification(shown, preview_context(owner))
    image_url = out.image_url.replace(settings.PUBLIC_URL + settings.MEDIA_URL, settings.MEDIA_URL, 1)
    return {
        "telegram_html": out.telegram_html.replace("\n", "<br>"),
        "push_body": out.push_body,
        "button_label": out.button_label,
        "button_url": out.button_url,
        "image_url": image_url,
        "fallback": used_fallback,
        "warnings": [readable_warning(w) for w in dict.fromkeys(out.warnings)] if is_app_template(owner) else [],
    }


def is_blank(content):
    return content is None or not (content.subject or content.preview_text or content.blocks or content.html
                                   or content.text or content.button_label or content.button_url or content.image)


def _image_url(content):
    if content is None or not content.image:
        return ""
    try:
        return content.image.url
    except ValueError:
        return ""


def editor_payload(owner, lang, form, sample=False):
    """Everything the page needs after a save: tab states, the canvas, the previews."""
    content = get_content(owner, lang, form)
    payload = {
        "ok": True,
        "langs": language_states(owner, form),
        "blank": is_blank(content),
        "customised": bool(getattr(owner, "customised", False)),
        "problems": {},
        "warnings": [],
    }
    if form == "email":
        shown = content or Content(language=lang, form="email")
        payload.update(render_canvas(owner, shown, sample))
        payload["problems"] = block_problems(shown.blocks)
        payload["mode"] = shown.mode
    else:
        default = get_content(owner, Language.default_code(), "notification") if lang != Language.default_code() else None
        payload["note"] = render_note(owner, content, default)
        payload["warnings"] = payload["note"].pop("warnings")
        payload["image_url"] = _image_url(content)
    return payload


def content_state(content, lang, form):
    """The stored fields, for the page's first load."""
    content = content or Content(language=lang, form=form)
    return {
        "subject": content.subject,
        "preview_text": content.preview_text,
        "mode": content.mode,
        "blocks": content.blocks or [],
        "html": content.html if content.mode == "html" else blocks_to_editable_html(content.blocks),
        "text": content.text,
        "button_label": content.button_label,
        "button_url": content.button_url,
        "image_url": _image_url(content),
    }


# --------------------------------------------------------------------------- pages

def _editor_page(request, owner, context):
    codes = Language.codes()
    default_code = Language.default_code()
    lang = request.GET.get("lang")
    if lang not in codes:
        lang = default_code
    form = request.GET.get("form")
    if form not in FORMS:
        form = "email"
    content = get_content(owner, lang, form)
    stored_mode = content.mode if content else "visual"
    view = request.GET.get("view")
    if form != "email":
        view = "visual"
    elif stored_mode == "html":
        # Content written as HTML opens as HTML; going back to blocks is a deliberate step.
        view = "html"
    elif view not in ("visual", "html"):
        view = "visual"
    can_edit = request.user.has_role("editor") and owner_editable(owner)
    app = is_app_template(owner)
    default_lang = Language.default()
    org = Organization.get()
    owner_id = owner.public_id

    state = editor_payload(owner, lang, form)
    state.update(content_state(content, lang, form))
    # The empty state offers "Copy from <default>", so the default language never shows it.
    state["blank"] = state["blank"] and lang != default_code
    state.update({
        "owner": owner_id,
        "kind": "campaign" if isinstance(owner, Campaign) else "template",
        "name": owner.name,
        "editable": can_edit,
        "lang": lang,
        "form": form,
        "view": view,
        "default_code": default_code,
        "placeholders": placeholder_list(owner),
        "allow_repeat": app,
        "limits": {"text": TELEGRAM_LIMIT, "caption": TELEGRAM_CAPTION_LIMIT},
        "csrf": get_token(request),
        "urls": {
            "save": reverse("content:editor_save", args=[owner_id]),
            "canvas": reverse("content:editor_canvas", args=[owner_id]),
            "upload": reverse("content:editor_upload", args=[owner_id]),
            "image": reverse("content:editor_image", args=[owner_id]),
            "copy": reverse("content:editor_copy", args=[owner_id]),
            "page": request.path,
        },
        "i18n": {
            "saved": _("Saved a moment ago"),
            "saving": _("Saving"),
            "unsaved": _("Not saved yet"),
            "failed": _("Could not save. Check your connection and try again."),
            "back_to_blocks": _("Go back to blocks? Your HTML changes will be lost."),
            "pick_field": _("Click into a text field first, then pick what to insert."),
            "upload_failed": _("That picture could not be uploaded."),
            "drop_here": _("Drop a block here to add it at the end"),
            "footer_note": (_("Footer, added automatically in each language") if not app
                            else _("Footer, added automatically. Service messages have no unsubscribe link.")),
            "default": _("Default"),
            "not_written": _("Not written"),
            "written_of": _("{n} of {total} languages written"),
            "showing_sample": _("Showing sample data"),
            "showing_tags": _("Showing placeholders"),
            "choose_image": _("Choose a picture"),
            "replace_image": _("Replace image"),
            "add_image": _("Add image"),
            "bold": _("bold"),
            "italic": _("italic"),
            "link_text": _("link text"),
            "block": _("block"),
            "types": {
                "heading": _("Heading"), "text": _("Text"), "image": _("Image"), "button": _("Button"),
                "card": _("Event card"), "divider": _("Divider"), "repeat": _("Repeating list"),
            },
            "defaults": {
                "heading": _("New heading"), "text": _("Write something here."), "button": _("Button label"),
                "card_title": _("Event name"), "card_text": _("When and where."),
            },
            "stubs": {
                "image": _("Image: choose a picture in the panel"),
                "text": _("Empty text block"),
                "repeat": _("Repeating list: pick what to repeat in the panel"),
            },
        },
    })
    readers = next((l["readers"] for l in state["langs"] if l["code"] == lang), 0)
    context.update({
        "owner": owner,
        "is_campaign": isinstance(owner, Campaign),
        "is_app": app,
        "can_edit": can_edit,
        "lang": lang,
        "form": form,
        "view": view,
        "current_language": next((l for l in Language.objects.all() if l.code == lang), default_lang),
        "default_language": default_lang,
        "default_language_name": (default_lang.english_name or default_lang.name) if default_lang else "English",
        "readers": readers,
        "state": state,
        "block_palette": [
            ("heading", _("Heading")), ("text", _("Text")), ("image", _("Image")), ("button", _("Button")),
            ("card", _("Event card")),
        ] + ([("repeat", _("Repeating list"))] if app else []) + [("divider", _("Divider"))],
        "placeholders": state["placeholders"],
        "org_name": org.name,
    })
    return render(request, "content/editor.html", context)


@role_required("editor")
def campaign_design(request, public_id):
    """Step 1 of a campaign. Routed by the campaigns area as campaigns:design."""
    campaign = Campaign.get_by_public_id(public_id)
    try:
        back_url = reverse("campaigns:list")
    except NoReverseMatch:
        back_url = "/campaigns/"
    return _editor_page(request, campaign, {
        "back_url": back_url,
        "back_label": _("Campaigns"),
        "send_url": f"/campaigns/{campaign.public_id}/send/",
    })


@role_required("viewer")
def template_editor(request, key):
    template = get_object_or_404(Template, key=key)
    context = {"back_url": reverse("content:templates"), "back_label": _("Templates"), "template": template}
    if template.kind == "app":
        from channels.models import EmailSender

        sender = EmailSender.app_sender()
        has_email, has_note = template.has_form("email"), template.has_form("notification")
        if has_email and has_note:
            goes_out = _("Email and Telegram")
        elif has_note:
            goes_out = _("Telegram")
        elif has_email:
            goes_out = _("Email")
        else:
            goes_out = _("Nothing written yet")
        context.update({"app_sender": sender, "goes_out": goes_out})
    return _editor_page(request, template, context)


# --------------------------------------------------------------------------- endpoints

def _refuse(message, status=400):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _editable_owner(owner_id):
    """(owner, refusal). A sent or sending campaign is read-only."""
    owner = resolve_owner(owner_id)
    if not owner_editable(owner):
        return owner, _refuse(_("This campaign has already gone out, so it cannot be changed."), 409)
    return owner, None


def _lang_and_form(data):
    lang, form = data.get("lang"), data.get("form")
    if lang not in Language.codes() or form not in FORMS:
        return None, None
    return lang, form


def _mark_customised(owner):
    if is_app_template(owner) and not owner.customised:
        owner.customised = True
        owner.save(update_fields=["customised", "updated_at"])


@role_required("editor")
@require_POST
def editor_save(request, owner):
    owner, refusal = _editable_owner(owner)
    if refusal:
        return refusal
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return _refuse(_("That did not arrive properly. Please try again."))
    if not isinstance(data, dict):
        return _refuse(_("That did not arrive properly. Please try again."))
    lang, form = _lang_and_form(data)
    if lang is None:
        return _refuse(_("Pick a language and a form first."))

    # Check the HTML before anything is stored, so a refused save changes nothing.
    if form == "email" and "html" in data:
        try:
            validate_jinja(str(data["html"] or ""))
        except TemplateNotAllowed as exc:
            return JsonResponse({"ok": False, "error": str(exc), "field": "html"})

    name = data.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != owner.name and not is_app_template(owner):
        owner.name = name.strip()[:160]
        owner.save(update_fields=["name", "updated_at"])

    content = get_content(owner, lang, form, create=True)
    if form == "email":
        if "subject" in data:
            content.subject = str(data["subject"] or "")[:300]
        if "preview_text" in data:
            content.preview_text = str(data["preview_text"] or "")[:300]
        if "html" in data:
            # Editing the HTML makes this content HTML (spec section 9).
            content.mode = "html"
            content.html = str(data["html"] or "")
        elif data.get("mode") == "visual":
            content.mode = "visual"
            content.html = ""
        if "blocks" in data and content.mode == "visual":
            content.blocks = clean_blocks(data["blocks"], allow_repeat=is_app_template(owner))
    else:
        if "text" in data:
            content.text = str(data["text"] or "")
        if "button_label" in data:
            content.button_label = str(data["button_label"] or "")[:120]
        if "button_url" in data:
            content.button_url = str(data["button_url"] or "").strip()[:500]
    content.save()
    _mark_customised(owner)
    return JsonResponse(editor_payload(owner, lang, form, sample=bool(data.get("sample"))))


@role_required("viewer")
@require_GET
def editor_canvas(request, owner):
    owner = resolve_owner(owner)
    if isinstance(owner, Campaign) and not request.user.has_role("editor"):
        return _refuse(_("Your role cannot open this."), 403)
    lang, form = _lang_and_form(request.GET)
    if lang is None:
        return _refuse(_("Pick a language and a form first."))
    return JsonResponse(editor_payload(owner, lang, form, sample=request.GET.get("sample") == "1"))


def _checked_image(upload):
    """(extension, error). Pictures only, at most 5 MB."""
    if upload is None:
        return None, _("Choose a picture first.")
    ext = os.path.splitext(upload.name or "")[1].lower()
    if ext not in IMAGE_TYPES:
        return None, _("Use a JPG, PNG, GIF or WebP picture.")
    if upload.size > MAX_IMAGE_BYTES:
        return None, _("That picture is larger than 5 MB. Please use a smaller one.")
    try:
        from PIL import Image

        Image.open(upload).verify()
        upload.seek(0)
    except ImportError:
        pass
    except Exception:
        return None, _("That file is not a picture we can read.")
    return ext, None


def _stored_name(upload, ext):
    stem = get_valid_filename(os.path.splitext(os.path.basename(upload.name))[0])[:60] or "image"
    return f"{secrets.token_hex(4)}-{stem}{ext}"


@role_required("editor")
@require_POST
def editor_upload(request, owner):
    """A picture for an image block. Stored in media; the block keeps the /media/... path."""
    owner, refusal = _editable_owner(owner)
    if refusal:
        return refusal
    upload = request.FILES.get("file")
    ext, error = _checked_image(upload)
    if error:
        return _refuse(error)
    name = default_storage.save("e/" + _stored_name(upload, ext), upload)
    return JsonResponse({"ok": True, "url": settings.MEDIA_URL + name.replace("\\", "/")})


@role_required("editor")
@require_POST
def editor_image(request, owner):
    """Attach or remove the notification's image."""
    owner, refusal = _editable_owner(owner)
    if refusal:
        return refusal
    lang = request.POST.get("lang")
    if lang not in Language.codes():
        return _refuse(_("Pick a language and a form first."))
    content = get_content(owner, lang, "notification", create=True)
    if request.POST.get("remove"):
        content.image = None
        content.save()
    else:
        upload = request.FILES.get("file")
        ext, error = _checked_image(upload)
        if error:
            return _refuse(error)
        content.image.save(_stored_name(upload, ext), upload, save=True)
    _mark_customised(owner)
    return JsonResponse(editor_payload(owner, lang, "notification"))


@role_required("editor")
@require_POST
def editor_copy(request, owner):
    """"Copy from English": fill this language from the default one."""
    owner, refusal = _editable_owner(owner)
    if refusal:
        return refusal
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = {}
    lang, form = _lang_and_form(data if isinstance(data, dict) else {})
    if lang is None:
        return _refuse(_("Pick a language and a form first."))
    copied = copy_language(owner, lang)
    if copied:
        _mark_customised(owner)
    payload = editor_payload(owner, lang, form)
    payload["copied"] = copied
    return JsonResponse(payload)
