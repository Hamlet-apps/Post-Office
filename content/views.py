"""The Templates screen and the quick message dialog."""
import json
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_POST

from core.models import Language
from core.permissions import role_required
from people.models import List, Person

from .models import Content, QuickMessage, Template
from .render import TELEGRAM_LIMIT
from .services import create_campaign_template, reset_to_default

# --------------------------------------------------------------------------- Templates screen


def _written_map(templates):
    """{template id: {form: set of language codes written}} in one query."""
    written = {}
    for content in Content.objects.filter(template__in=templates):
        if content.is_written:
            written.setdefault(content.template_id, {}).setdefault(content.form, set()).add(content.language)
    return written


def _language_tags(forms, languages):
    """One tag per organization language; written means every form the message has is written."""
    tags = []
    for lang in languages:
        if forms:
            written = all(lang.code in codes for codes in forms.values())
        else:
            written = False
        tags.append({"code": lang.code, "label": lang.code.upper(), "name": lang.english_name or lang.name, "written": written})
    return tags


def new_campaign_url():
    try:
        return reverse("campaigns:new")
    except NoReverseMatch:
        return "/campaigns/new/"


@role_required("viewer")
def templates(request):
    q = (request.GET.get("q") or "").strip()
    languages = list(Language.objects.all())
    all_templates = Template.objects.select_related("building")
    if q:
        all_templates = all_templates.filter(Q(name__icontains=q) | Q(fires__icontains=q) | Q(key__icontains=q))
    all_templates = list(all_templates)
    written = _written_map(all_templates)

    from delivery.models import Message

    since = timezone.now() - timedelta(days=30)
    sent = dict(
        Message.objects.filter(template__in=all_templates, created_at__gte=since)
        .values_list("template").annotate(n=Count("id"))
    )

    campaign_templates, groups = [], {}
    for template in all_templates:
        forms = written.get(template.pk, {})
        template.language_tags = _language_tags(forms, languages)
        template.has_email = bool(forms.get("email"))
        template.has_telegram = bool(forms.get("notification"))
        template.sent_30 = sent.get(template.pk, 0)
        if template.kind == "campaign":
            campaign_templates.append(template)
        else:
            group = groups.setdefault(template.building_id, {"building": template.building, "templates": []})
            group["templates"].append(template)
    campaign_templates.sort(key=lambda t: t.name.lower())

    return render(request, "content/templates.html", {
        "q": q,
        "campaign_templates": campaign_templates,
        "groups": list(groups.values()),
        "can_edit": request.user.has_role("editor"),
        "new_campaign_url": new_campaign_url(),
    })


@role_required("editor")
@require_POST
def template_new(request):
    """A new, empty campaign template."""
    template = create_campaign_template(request.POST.get("name") or _("Untitled template"))
    return redirect("content:template", key=template.key)


@role_required("editor")
@require_POST
def template_reset(request, key):
    template = get_object_or_404(Template, key=key, kind="app")
    reset_to_default(template)
    messages.success(request, _("%(name)s is back to the version %(building)s sent.") % {
        "name": template.name, "building": template.building.name if template.building else _("the building")})
    if request.POST.get("next") == "editor":
        return redirect("content:template", key=template.key)
    return redirect("content:templates")


@role_required("editor")
@require_POST
def template_delete(request, key):
    """Campaign templates only; app messages belong to their building."""
    template = get_object_or_404(Template, key=key, kind="campaign")
    name = template.name
    template.delete()
    messages.success(request, _("%(name)s was deleted.") % {"name": name})
    return redirect("content:templates")


# --------------------------------------------------------------------------- Quick message

def _list_label(lst, count):
    return f"{lst.display_name()} · {count:,}"


def _chosen(request_data):
    """(lists, people) picked in the dialog, from public ids."""
    list_pks = [List.pk_from_public_id(v) for v in request_data.getlist("lists")]
    person_pks = [Person.pk_from_public_id(v) for v in request_data.getlist("people")]
    lists = list(List.objects.filter(pk__in=[pk for pk in list_pks if pk]))
    people = list(Person.objects.filter(pk__in=[pk for pk in person_pks if pk]))
    return lists, people


def _recipients(lists, people):
    """Everyone the message would go to, counted once."""
    pks = {p.pk for p in people}
    for lst in lists:
        pks.update(lst.people().values_list("pk", flat=True))
    return Person.objects.filter(pk__in=pks)


def _message_type(lists):
    """A campaign list makes it a newsletter (consent rules apply); otherwise it is a service message."""
    return "newsletter" if any(lst.kind == "campaign" for lst in lists) else "service"


def _texts(request_data, languages):
    return {lang.code: (request_data.get(f"text_{lang.code}") or "").strip() for lang in languages}


def _summary(lists, people, texts, languages):
    recipients = _recipients(lists, people)
    by_language = {}
    for code in recipients.values_list("language__code", flat=True):
        by_language[code] = by_language.get(code, 0) + 1
    total = sum(by_language.values())
    default = next((l for l in languages if l.is_default), languages[0] if languages else None)
    default_name = (default.english_name or default.name) if default else "English"
    warnings = []
    for lang in languages:
        if lang == default or texts.get(lang.code):
            continue
        count = by_language.get(lang.code, 0)
        if count:
            name = lang.english_name or lang.name
            warnings.append(ngettext(
                "%(language)s is not written. %(count)s %(language)s reader will get the %(default)s version.",
                "%(language)s is not written. %(count)s %(language)s readers will get the %(default)s version.",
                count) % {"language": name, "count": f"{count:,}", "default": default_name})
    message_type = _message_type(lists)
    return {
        "total": total,
        "warnings": warnings,
        "message_type": message_type,
        "type_label": _("counted as a newsletter, so consent rules apply") if message_type == "newsletter"
        else _("counted as a service message"),
    }


def _dialog(request, *, lists=(), people=(), texts=None, channel="", error="", status=200):
    languages = list(Language.objects.all())
    texts = texts or {lang.code: "" for lang in languages}
    chips = [{"kind": "lists", "id": lst.public_id, "label": _list_label(lst, lst.people().count())} for lst in lists]
    chips += [{"kind": "people", "id": p.public_id, "label": p.display_name} for p in people]
    return render(request, "content/quick_message.html", {
        "languages": languages,
        "default_code": Language.default_code(),
        "chips_json": json.dumps(chips, ensure_ascii=False),
        "texts_json": json.dumps(texts, ensure_ascii=False),
        "channel": channel if channel in ("email", "telegram", "push") else "",
        "channel_options": [("", _("Every channel they use")), ("email", _("Email")), ("telegram", _("Telegram")), ("push", _("Push"))],
        "summary": _summary(list(lists), list(people), texts, languages),
        "error": error,
        "limit": TELEGRAM_LIMIT,
    }, status=status)


@role_required("editor")
def quick_message(request):
    """GET: the dialog fragment. POST: send now."""
    languages = list(Language.objects.all())
    if request.method != "POST":
        lists = [List.get_by_public_id(request.GET["list"])] if request.GET.get("list") else []
        people = [Person.get_by_public_id(request.GET["person"])] if request.GET.get("person") else []
        return _dialog(request, lists=lists, people=people)

    lists, people = _chosen(request.POST)
    texts = _texts(request.POST, languages)
    channel = request.POST.get("channel") or ""
    if channel not in ("", "email", "telegram", "push"):
        channel = ""
    default_code = Language.default_code()
    default_name = next(((l.english_name or l.name) for l in languages if l.code == default_code), "English")
    error = ""
    if not lists and not people:
        error = _("Add at least one person or list to send to.")
    elif not texts.get(default_code):
        error = _("Write the message in %(language)s first. Other languages fall back to it.") % {"language": default_name}
    elif any(len(t) > TELEGRAM_LIMIT for t in texts.values()):
        error = _("The message is too long. Telegram allows 4,096 characters.")
    elif not _recipients(lists, people).exists():
        error = _("Nobody is on those lists yet, so there is no one to send to.")
    if error:
        return _dialog(request, lists=lists, people=people, texts=texts, channel=channel, error=error)

    quick = QuickMessage.objects.create(
        created_by=request.user,
        channels=[channel] if channel else [],
        message_type=_message_type(lists),
    )
    quick.lists.set(lists)
    quick.people.set(people)
    for code, text in texts.items():
        if text:
            Content.objects.create(quick_message=quick, language=code, form="notification", text=text)

    from delivery import pipeline  # built by the delivery area; imported late on purpose

    pipeline.send_quick_message(quick)
    quick.refresh_from_db()
    if quick.sent_at is None:
        quick.sent_at = timezone.now()
        quick.save(update_fields=["sent_at", "updated_at"])

    total = _recipients(lists, people).count()
    toast = ngettext("Message on its way to %(count)s person.", "Message on its way to %(count)s people.", total) % {"count": f"{total:,}"}
    response = HttpResponse("")
    response["HX-Trigger"] = json.dumps({"po-toast": toast})
    return response


@role_required("editor")
@require_GET
def quick_message_search(request):
    """People and lists matching what was typed, as buttons that add a chip."""
    q = (request.GET.get("q") or "").strip()
    lists, people = [], []
    if q:
        needle = q.lower()
        for lst in List.objects.all():
            names = lst.name.values() if isinstance(lst.name, dict) else [str(lst.name)]
            if any(needle in str(n).lower() for n in names) or needle in lst.slug.lower():
                lists.append({"list": lst, "label": _list_label(lst, lst.people().count())})
        lists = lists[:6]
        query = Q()
        for word in q.split()[:3]:
            query &= Q(first_name__icontains=word) | Q(last_name__icontains=word) | Q(email__icontains=word)
        people = list(Person.objects.filter(query)[:8])
    return render(request, "content/quick_message_results.html", {"q": q, "lists": lists, "people": people})


@role_required("editor")
@require_POST
def quick_message_summary(request):
    """The recipient count and the fallback warnings, refreshed as the dialog changes."""
    languages = list(Language.objects.all())
    lists, people = _chosen(request.POST)
    return render(request, "content/quick_message_summary.html", {
        "summary": _summary(lists, people, _texts(request.POST, languages), languages),
    })
