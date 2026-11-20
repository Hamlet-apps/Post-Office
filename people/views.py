"""Staff screens for people and lists (spec section 11): People, Person, import, signup form settings.

Viewers can look; changing anything needs the editor role. App lists and their
members belong to the building that keeps them, so staff cannot edit them here.
"""
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop as N_
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods, require_POST

from core.models import Language, Organization
from core.permissions import role_required

from . import importer, services
from .models import CHANNELS, ConsentRecord, List, Person, Segment, Subscription, normalize_email
from .tokens import connect_url

PER_PAGE = 25
HISTORY_SHORT = 12
HISTORY_LONG = 200

STATUSES = [
    # value, label, tag class
    ("subscribed", N_("Subscribed"), "po-tag--ok"),
    ("unconfirmed", N_("Waiting to confirm"), "po-tag--peach"),
    ("service", N_("Service messages only"), ""),
    ("unsubscribed", N_("Unsubscribed"), ""),
    ("none", N_("Not on any list"), ""),
    ("bounced", N_("Email bounced"), "po-tag--danger"),
    ("complained", N_("Marked us as spam"), "po-tag--danger"),
]


def _channel_label(channel):
    return {"email": _("Email"), "telegram": _("Telegram"), "push": _("Push")}[channel]


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _with_status(qs):
    subs = Subscription.objects.filter(person=OuterRef("pk"))
    return qs.annotate(
        has_news=Exists(subs.filter(list__kind="campaign", state="on")),
        has_unconfirmed=Exists(subs.filter(state="unconfirmed")),
        has_service=Exists(subs.filter(list__kind="app", state="on")),
        has_off=Exists(subs.filter(list__kind="campaign", state="off")),
    )


def _status(person):
    """(value, label, tag class) for a person annotated by _with_status."""
    if person.email_state == "bounced":
        value = "bounced"
    elif person.email_state == "complained":
        value = "complained"
    elif person.has_news:
        value = "subscribed"
    elif person.has_unconfirmed:
        value = "unconfirmed"
    elif person.has_service:
        value = "service"
    elif person.has_off:
        value = "unsubscribed"
    else:
        value = "none"
    return next((v, _(label), css) for v, label, css in STATUSES if v == value)


def _filter_status(qs, value):
    """The same precedence as _status, as a query."""
    if value in ("bounced", "complained"):
        return qs.filter(email_state=value)
    qs = qs.filter(email_state="ok")
    if value == "subscribed":
        return qs.filter(has_news=True)
    qs = qs.filter(has_news=False)
    if value == "unconfirmed":
        return qs.filter(has_unconfirmed=True)
    qs = qs.filter(has_unconfirmed=False)
    if value == "service":
        return qs.filter(has_service=True)
    qs = qs.filter(has_service=False)
    if value == "unsubscribed":
        return qs.filter(has_off=True)
    return qs.filter(has_off=False)


def _filter_channel(qs, channel):
    from channels.models import PushMember, TelegramLink

    if channel == "email":
        return qs.filter(email__isnull=False, email_state="ok")
    if channel == "telegram":
        return qs.filter(Exists(TelegramLink.objects.filter(person=OuterRef("pk"), blocked_at__isnull=True)))
    return qs.filter(Exists(PushMember.objects.filter(person=OuterRef("pk"))))


def _initials(person):
    name = person.full_name or person.email or "?"
    parts = name.replace("@", " ").split()
    return "".join(p[0] for p in parts[:2]).upper()


# --------------------------------------------------------------------------- People

@role_required("viewer")
def list_view(request):
    org = Organization.get()
    default_code = Language.default_code()
    languages = list(Language.objects.all())
    q = request.GET.get("q", "").strip()
    lang = request.GET.get("lang", "")
    channel = request.GET.get("channel", "")
    status = request.GET.get("status", "")
    current_list = None
    if request.GET.get("list"):
        current_list = List.objects.filter(pk=List.pk_from_public_id(request.GET["list"])).select_related("building").first()

    total = Person.objects.count()
    qs = _with_status(Person.objects.all()).select_related("language")
    if current_list:
        qs = qs.filter(Exists(Subscription.objects.filter(person=OuterRef("pk"), list=current_list, state="on")))
    if q:
        cond = Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        parts = q.split()
        if len(parts) > 1:
            cond |= Q(first_name__icontains=parts[0], last_name__icontains=" ".join(parts[1:]))
        qs = qs.filter(cond)
    if lang and any(l.code == lang for l in languages):
        qs = qs.filter(language__code=lang)
    if channel in CHANNELS:
        qs = _filter_channel(qs, channel)
    if status and any(status == s[0] for s in STATUSES):
        qs = _filter_status(qs, status)
    qs = qs.order_by("-created_at", "-id").prefetch_related("subscriptions__list", "telegram_links", "push_memberships")

    paginator = Paginator(qs, PER_PAGE)
    page = paginator.get_page(request.GET.get("page"))
    people = list(page.object_list)
    for person in people:
        person.status = _status(person)
        person.initials = _initials(person)
        reach = []
        if person.email_reachable:
            reach.append("email")
        if any(link.blocked_at is None for link in person.telegram_links.all()):
            reach.append("telegram")
        if person.push_memberships.all():
            reach.append("push")
        person.reach = [{"channel": c, "label": _channel_label(c), "icon": "mail" if c == "email" else c} for c in reach]
        names = []
        for sub in person.subscriptions.all():
            if sub.state == "on":
                name = sub.list.display_name(default_code)
                if name not in names:
                    names.append(name)
        person.list_names = ", ".join(names)

    counts = dict(Subscription.objects.filter(state="on").values_list("list").annotate(n=Count("person", distinct=True)))
    lists = list(List.objects.select_related("building"))
    for lst in lists:
        lst.count = counts.get(lst.pk, 0)
        lst.label = lst.display_name(default_code)
        lst.note = lst.display_description(default_code)
        lst.current = bool(current_list and current_list.pk == lst.pk)
    filtered = bool(q or lang or channel or status)
    context = {
        "org": org, "total": total, "people": people, "page": page,
        "pages": list(paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1)) if paginator.num_pages > 1 else [],
        "ellipsis": Paginator.ELLIPSIS,
        "campaign_lists": [l for l in lists if l.kind == "campaign"],
        "app_lists": [l for l in lists if l.kind == "app"],
        "segments": Segment.objects.all(),
        "current_list": current_list, "current_list_name": current_list.display_name(default_code) if current_list else "",
        "q": q, "lang": lang, "channel": channel, "status": status, "filtered": filtered,
        "languages": languages,
        "channel_choices": [(c, _channel_label(c)) for c in CHANNELS],
        "status_choices": [(v, _(label)) for v, label, _css in STATUSES],
        "can_edit": request.user.can_edit,
    }
    return render(request, "people/list.html", context)


# --------------------------------------------------------------------------- lists

def _campaign_list_or_403(public_id):
    lst = List.get_by_public_id(public_id)
    if lst.kind != "campaign":
        raise PermissionDenied("App lists are kept by their building and cannot be changed here.")
    return lst


def _unique_slug(base, exclude_pk=None):
    base = slugify(base)[:70] or "list"
    slug, n = base, 2
    while List.objects.filter(slug=slug).exclude(pk=exclude_pk).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _list_dialog(request, lst):
    languages = list(Language.objects.all())
    default = Language.default()
    errors = []
    values = {
        "name": dict(lst.name or {}) if lst else {},
        "description": dict(lst.description or {}) if lst else {},
        "slug": lst.slug if lst else "",
        "on_signup_form": lst.on_signup_form if lst else True,
    }
    if request.method == "POST":
        for lang in languages:
            for key in ("name", "description"):
                text = request.POST.get(f"{key}_{lang.code}", "").strip()
                if text:
                    values[key][lang.code] = text
                else:
                    values[key].pop(lang.code, None)
        values["slug"] = request.POST.get("slug", "").strip().lower()
        values["on_signup_form"] = bool(request.POST.get("on_signup_form"))
        default_name = values["name"].get(default.code if default else "en", "")
        if not default_name:
            errors.append(_("Give the list a name in %(language)s.") % {"language": default.name if default else "English"})
        slug = values["slug"]
        if slug and slug != slugify(slug):
            errors.append(_("The short name can only use lower-case letters, numbers and hyphens."))
        elif slug and List.objects.filter(slug=slug).exclude(pk=lst.pk if lst else None).exists():
            errors.append(_("Another list already uses this short name."))
        if not errors:
            if lst is None:
                position = (List.objects.aggregate(m=Max("position"))["m"] or 0) + 1
                lst = List(kind="campaign", position=position)
            lst.name = values["name"]
            lst.description = values["description"]
            lst.slug = slug or _unique_slug(default_name, lst.pk)
            lst.on_signup_form = values["on_signup_form"]
            lst.save()
            messages.success(request, _("List saved."))
            url = f"{reverse('people:list')}?list={lst.public_id}"
            if _is_htmx(request):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = url
                return response
            return redirect(url)
    rows = [{"lang": lang, "name": values["name"].get(lang.code, ""), "description": values["description"].get(lang.code, ""),
             "is_default": bool(default and lang.pk == default.pk)} for lang in languages]
    members = lst.people().count() if lst and lst.pk else 0
    return render(request, "people/_list_dialog.html", {
        "lst": lst if lst and lst.pk else None, "rows": rows, "values": values, "errors": errors, "members": members})


@role_required("editor")
@require_http_methods(["GET", "POST"])
def list_new(request):
    return _list_dialog(request, None)


@role_required("editor")
@require_http_methods(["GET", "POST"])
def list_edit(request, public_id):
    return _list_dialog(request, _campaign_list_or_403(public_id))


@role_required("editor")
@require_POST
def list_delete(request, public_id):
    lst = _campaign_list_or_403(public_id)
    name = lst.display_name()
    lst.delete()
    messages.success(request, _("%(name)s was deleted.") % {"name": name})
    return redirect("people:list")


# --------------------------------------------------------------------------- import

def _import_key(token):
    return f"people-import:{token}"


@role_required("editor")
@require_http_methods(["GET", "POST"])
def import_view(request):
    default_code = Language.default_code()
    lists = list(List.objects.filter(kind="campaign"))
    for lst in lists:
        lst.label = lst.display_name(default_code)
    context = {"lists": lists, "wording": importer.import_wording(), "chosen": request.POST.get("list", request.GET.get("list", ""))}
    if request.method == "GET":
        return render(request, "people/import.html", context)

    agreed = bool(request.POST.get("agreed"))
    target = next((l for l in lists if l.public_id == request.POST.get("list")), None)
    token = request.POST.get("token", "")
    stored = cache.get(_import_key(token)) if token else None
    upload = request.FILES.get("file")

    def refuse(message, status=400):
        context["error"] = message
        return render(request, "people/import.html", context, status=status)

    if not agreed:
        return refuse(_("Tick the box to say these people agreed to receive this. Without it nothing is imported."))
    if target is None:
        return refuse(_("Choose a campaign list to add these people to."))
    try:
        if upload is not None:
            if upload.size > importer.MAX_BYTES:
                raise importer.ImportError_(_("This file is larger than 5 MB. Split it into smaller files."))
            stored = {"data": upload.read(), "file_name": upload.name, "user": request.user.pk}
            token = secrets.token_urlsafe(16)
            cache.set(_import_key(token), stored, 60 * 60)
        elif stored is None or stored.get("user") != request.user.pk:
            return refuse(_("Choose the CSV file again."))
        parsed = importer.parse_csv(stored["data"])
        if request.POST.get("action") == "commit" and upload is None:
            plan = importer.run_import(parsed, target, consent_given=agreed, by=request.user, file_name=stored["file_name"])
            cache.delete(_import_key(token))
            parts = [ngettext("%(n)d new person", "%(n)d new people", plan.new) % {"n": plan.new},
                     _("%(n)d updated") % {"n": plan.updated}, _("%(n)d skipped") % {"n": plan.skipped}]
            messages.success(request, _("Import finished: %(summary)s.") % {"summary": ", ".join(parts)})
            return redirect(f"{reverse('people:list')}?list={target.public_id}")
        plan = importer.plan_import(parsed, target)
    except (importer.ImportError_, services.ConsentRequired) as exc:
        return refuse(str(exc))

    preview = []
    for row, action in list(zip(parsed.rows, plan.actions))[:10]:
        preview.append({**row, "extras": [row["attributes"].get(c, "") for c in parsed.extra_columns],
                        "action": action, "action_label": importer.action_label(action),
                        "skipped": action not in ("new", "updated")})
    context.update({
        "parsed": parsed, "plan": plan, "preview": preview, "token": token, "target": target,
        "file_name": stored["file_name"],
        "reasons": [(importer.skip_reason_label(r), n) for r, n in plan.reasons.items()],
        "importable": plan.new + plan.updated,
    })
    return render(request, "people/import_preview.html", context)


# --------------------------------------------------------------------------- add a person

@role_required("editor")
@require_http_methods(["GET", "POST"])
def person_new(request):
    languages = list(Language.objects.all())
    values = {"first_name": "", "last_name": "", "email": "", "language": Language.default_code()}
    errors = []
    if request.method == "POST":
        for key in values:
            values[key] = request.POST.get(key, "").strip()
        email = normalize_email(values["email"])
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors.append(_("That does not look like an email address."))
            else:
                if Person.objects.filter(email=email).exists():
                    errors.append(_("Someone with this email address is already here."))
        if not email and not (values["first_name"] or values["last_name"]):
            errors.append(_("Give a name or an email address."))
        if not errors:
            person = Person(email=email, first_name=values["first_name"][:120], last_name=values["last_name"][:120],
                            language=Language.objects.filter(code=values["language"]).first(), source="staff")
            person.save()
            messages.success(request, _("Person added. Tick the lists they agreed to below."))
            url = reverse("people:person", args=[person.public_id])
            if _is_htmx(request):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = url
                return response
            return redirect(url)
    return render(request, "people/_person_dialog.html", {"values": values, "errors": errors, "languages": languages})


# --------------------------------------------------------------------------- Person

def _when(dt, org, with_time=True):
    local = timezone.localtime(dt, org.tz)
    return formats.date_format(local, "M j, Y, g:i A" if with_time else "M j, Y")


def consent_sentence(consent, org=None):
    """The proof of consent, in one sentence staff can read out."""
    if consent is None:
        return _("No consent is on record for this person.")
    org = org or Organization.get()
    when = _when(consent.given_at, org)
    by = str(consent.recorded_by) if consent.recorded_by_id else _("a team member")
    if consent.method == "signup_form":
        if consent.link_params and any(k in consent.link_params for k in ("name", "email", "lists", "telegram")):
            text = _("Pressed Subscribe on a pre-filled signup link on %(when)s") % {"when": when}
        elif (consent.link_params or {}).get("source") == "preferences":
            text = _("Switched this on themselves on their preferences page on %(when)s") % {"when": when}
        else:
            text = _("Ticked the consent box on the signup form on %(when)s") % {"when": when}
    elif consent.method == "import":
        text = _("Added by import on %(when)s by %(by)s, who confirmed these people had agreed") % {"when": when, "by": by}
    elif consent.method == "api":
        text = _("Gave consent through a connected app or website on %(when)s") % {"when": when}
    else:
        text = _("Recorded by %(by)s on %(when)s") % {"when": when, "by": by}
    if consent.method in ("signup_form", "api"):
        if consent.confirmed_at:
            text += _(", then confirmed by email on %(when)s") % {"when": _when(consent.confirmed_at, org)}
        elif consent.subscriptions.filter(state="unconfirmed").exists():
            text += _(", and has not confirmed by email yet")
    text += "."
    if consent.note:
        text += " " + _("Note: %(note)s") % {"note": consent.note}
    return text


def _grid_context(person, user):
    default_code = Language.default_code()
    grid = services.display_grid(person)
    for kind in ("campaign", "app"):
        for row in grid[kind]:
            lst = row["list"]
            row["name"] = lst.display_name(default_code)
            row["description"] = lst.display_description(default_code)
            for cell in row["cells"]:
                cell["label"] = _("%(list)s by %(channel)s") % {"list": row["name"], "channel": _channel_label(cell["channel"])}
    grid["column_labels"] = [_channel_label(c) for c in grid["columns"]]
    return {"person": person, "grid": grid, "can_edit": user.can_edit}


def _history(person, org, limit):
    events = []
    msgs = person.messages.select_related("campaign", "template", "building", "quick_message").order_by("-created_at")[:limit + 1]
    more = len(msgs) > limit
    sources = {"campaign": _("Campaign"), "quick": _("Quick message"), "system": "Post Office"}
    outbox = reverse("delivery:outbox")
    for m in list(msgs)[:limit]:
        source = m.building.name if m.source == "building" and m.building_id else sources.get(m.source, _("App"))
        failed = m.state == "failed"
        events.append({
            "at": m.sent_at or m.created_at, "title": m.title, "sub": f"{source} · {_channel_label(m.channel)}",
            "tag": _("Clicked a link") if m.state == "clicked" else m.get_state_display(),
            "tag_class": "po-tag--danger" if failed else ("po-tag--ok" if m.is_delivered else ""),
            "error": m.error if failed else "",
            "url": f"{outbox}?{urlencode({'status': 'failed', 'q': person.email or person.full_name})}" if failed else "",
        })
    methods = dict(ConsentRecord.METHOD_CHOICES)
    for c in person.consents.all():
        events.append({"at": c.given_at, "title": _("Agreed to receive messages"), "sub": _(methods.get(c.method, c.method)),
                       "tag": _("Consent"), "tag_class": "po-tag--teal", "error": "", "url": ""})
        if c.confirmed_at and c.method in ("signup_form", "api") and c.confirmed_at != c.given_at:
            events.append({"at": c.confirmed_at, "title": _("Confirmed their email address"), "sub": _(methods.get(c.method, c.method)),
                           "tag": _("Confirmed"), "tag_class": "po-tag--teal", "error": "", "url": ""})
    events.sort(key=lambda e: e["at"], reverse=True)
    return events, more


def _reach(person):
    from channels.models import TelegramBot

    rows = []
    if not person.email:
        rows.append({"icon": "mail", "name": _("Email"), "detail": _("No address"), "state": _("Not set"), "css": ""})
    else:
        state, css = {"ok": (_("Working"), "po-tag--ok"), "bounced": (_("Bounced"), "po-tag--danger"),
                      "complained": (_("Marked us as spam"), "po-tag--danger")}[person.email_state]
        rows.append({"icon": "mail", "name": _("Email"), "detail": person.email, "state": state, "css": css,
                     "clear_bounce": person.email_state == "bounced"})
    links = list(person.telegram_links.select_related("bot"))
    for link in links:
        detail = _("Connected %(date)s to %(bot)s") % {"date": formats.date_format(link.linked_at, "M j, Y"), "bot": link.bot}
        rows.append({"icon": "telegram", "name": _("Telegram"), "detail": detail,
                     "state": _("Blocked the bot") if link.blocked_at else _("Working"),
                     "css": "po-tag--danger" if link.blocked_at else "po-tag--ok"})
    bot = TelegramBot.default()
    if not links and bot is not None:
        rows.append({"icon": "telegram", "name": _("Telegram"), "detail": _("Not connected"), "state": _("Not set"), "css": "",
                     "connect": bool(bot.username)})
    for member in person.push_memberships.select_related("push_app__building"):
        rows.append({"icon": "push", "name": _("Push"), "detail": f"{member.push_app.building.name}, {member.push_app.label}",
                     "state": _("Working"), "css": "po-tag--ok"})
    return rows


@role_required("viewer")
@require_http_methods(["GET", "POST"])
def person_view(request, public_id):
    person = Person.get_by_public_id(public_id, _with_status(Person.objects.select_related("language")))
    org = Organization.get()
    errors = []
    if request.method == "POST":
        if not request.user.can_edit:
            raise PermissionDenied("Your role cannot change people.")
        email = normalize_email(request.POST.get("email"))
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors.append(_("That does not look like an email address."))
            else:
                if Person.objects.filter(email=email).exclude(pk=person.pk).exists():
                    errors.append(_("Someone else already has this email address."))
        attributes = {}
        for key, value in zip(request.POST.getlist("attr_key"), request.POST.getlist("attr_value")):
            key, value = key.strip(), value.strip()
            if key:
                attributes[key] = value
        person.first_name = request.POST.get("first_name", "").strip()[:120]
        person.last_name = request.POST.get("last_name", "").strip()[:120]
        language = Language.objects.filter(code=request.POST.get("language")).first()
        if language:
            person.language = language
        person.attributes = attributes
        if not errors:
            if email != person.email:
                person.email = email
                person.email_state = "ok"
            person.save()
            messages.success(request, _("Saved."))
            return redirect("people:person", public_id=person.public_id)

    limit = HISTORY_LONG if request.GET.get("history") == "all" else HISTORY_SHORT
    history, more = _history(person, org, limit)
    consent = person.consents.select_related("recorded_by").first()
    sources = {"signup_form": _("through the signup form"), "import": _("by import"), "api": _("through a connected app"),
               "staff": _("added by a team member")}
    joined = formats.date_format(timezone.localtime(person.created_at, org.tz), "M j, Y")
    context = {
        "person": person, "status": _status(person), "initials": _initials(person), "errors": errors,
        "languages": Language.objects.all(),
        "attributes": [{"key": k, "value": v} for k, v in (person.attributes or {}).items()],
        "reach": _reach(person),
        "consent": consent, "consent_sentence": consent_sentence(consent, org),
        "show_proof": org.keep_consent_proof,
        "history": history, "history_more": more,
        "joined": joined, "joined_how": sources.get(person.source) or (
            _("through %(source)s") % {"source": person.source} if person.source else ""),
        **_grid_context(person, request.user),
    }
    return render(request, "people/person.html", context, status=400 if errors else 200)


@role_required("editor")
@require_http_methods(["GET", "POST"])
def person_subscription(request, public_id):
    """Tick or untick one box in the person's grid.

    Switching off is free. Switching a newsletter on needs a note saying how the
    person gave consent; GET returns the dialog that asks for it.
    """
    person = Person.get_by_public_id(public_id)
    source = request.POST if request.method == "POST" else request.GET
    lst = List.get_by_public_id(source.get("list", ""))
    channel = source.get("channel", "")
    if channel not in CHANNELS:
        return HttpResponseBadRequest("Unknown channel")
    if lst.kind != "campaign":
        raise PermissionDenied("App lists are kept by their building; their members cannot be changed here.")
    dialog = {"person": person, "lst": lst, "list_name": lst.display_name(), "channel": channel,
              "channel_label": _channel_label(channel), "error": "", "note": ""}
    if request.method == "GET":
        return render(request, "people/_consent_dialog.html", dialog)

    turning_on = request.POST.get("on") == "1"
    if turning_on:
        note = request.POST.get("note", "").strip()
        try:
            consent = services.record_consent(
                person, [lst], "staff", _("A team member recorded that this person agreed to receive these messages."),
                note=note, recorded_by=request.user, confirmed=True)
        except services.ConsentRequired:
            dialog.update(error=_("Say how this person gave consent. Without a note the box stays off."), note=note)
            return render(request, "people/_consent_dialog.html", dialog, status=200 if _is_htmx(request) else 400)
        services.set_subscription(person, lst, channel, True, consent=consent)
    else:
        services.set_subscription(person, lst, channel, False)
    if not _is_htmx(request):
        return redirect("people:person", public_id=person.public_id)
    context = _grid_context(person, request.user)
    context["oob"] = turning_on  # the dialog form targets the dialog slot; the grid swaps itself in
    return render(request, "people/_grid.html", context)


@role_required("editor")
@require_POST
def person_clear_bounce(request, public_id):
    person = Person.get_by_public_id(public_id)
    if person.email_state == "bounced":
        person.email_state = "ok"
        person.save(update_fields=["email_state", "updated_at"])
        messages.success(request, _("Email is marked as working again. The next message will try this address."))
    return redirect("people:person", public_id=person.public_id)


@role_required("editor")
@require_POST
def person_connect_link(request, public_id):
    from channels.models import TelegramBot

    person = Person.get_by_public_id(public_id)
    bot = TelegramBot.default()
    url = connect_url(person, bot) if bot is not None and bot.username else ""
    return render(request, "people/_connect_link.html", {"url": url, "bot": bot})


@role_required("editor")
@require_POST
def person_stop_news(request, public_id):
    person = Person.get_by_public_id(public_id)
    count = services.unsubscribe_all_news(person)
    messages.success(request, _("All newsletters are stopped for this person.") if count else _("They had no newsletters switched on."))
    return redirect("people:person", public_id=person.public_id)


@role_required("editor")
@require_POST
def person_delete(request, public_id):
    person = Person.get_by_public_id(public_id)
    name = person.display_name
    services.delete_person(person)
    messages.success(request, _("%(name)s was deleted.") % {"name": name})
    return redirect("people:list")


# --------------------------------------------------------------------------- signup form settings

SIGNUP_OPTIONS = ("show_language", "show_telegram", "show_first_name")


@role_required("editor")
@require_http_methods(["GET", "POST"])
def signup_form(request):
    from channels.models import TelegramBot

    org = Organization.get()
    default_code = Language.default_code()
    lists = list(List.objects.filter(kind="campaign"))
    if request.method == "POST":
        chosen = set(request.POST.getlist("lists"))
        for lst in lists:
            on = lst.slug in chosen
            if lst.on_signup_form != on:
                lst.on_signup_form = on
                lst.save(update_fields=["on_signup_form", "updated_at"])
        options = dict(org.signup_form or {})
        for key in SIGNUP_OPTIONS:
            options[key] = bool(request.POST.get(key))
        org.signup_form = options
        org.save(update_fields=["signup_form", "updated_at"])
        messages.success(request, _("Signup form saved."))
        return redirect("people:signup_form")
    languages = list(Language.objects.all())
    for lst in lists:
        lst.label = lst.display_name(default_code)
        lst.note = lst.display_description(default_code)
    has_bot = TelegramBot.objects.exists()
    join_url = f"{settings.PUBLIC_URL}/join"
    config = {
        "joinUrl": join_url,
        "previewUrl": reverse("public:join"),
        "lists": {lst.slug: lst.on_signup_form for lst in lists},
        "showLanguage": bool(org.signup_option("show_language")),
        "showTelegram": bool(org.signup_option("show_telegram")),
        "showFirstName": bool(org.signup_option("show_first_name")),
        "hasBot": has_bot,
    }
    return render(request, "people/signup_form.html", {
        "lists": lists, "languages": languages, "has_bot": has_bot, "join_url": join_url, "config": config,
        "language_count": len(languages),
    })
