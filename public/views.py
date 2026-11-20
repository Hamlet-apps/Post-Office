"""Pages people (not staff) see: the signup form, email confirmation, preferences and unsubscribe.

Nobody signs in here. A person is named by a signed token (people.tokens) or by a
message's token. All wording is rendered in the reader's language.
"""
import ipaddress
import secrets
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _
from django.utils.translation.trans_real import parse_accept_lang_header
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from core.models import Language, Organization
from people import services
from people.models import CHANNELS, ConsentRecord, List
from people.signup import consent_wording, process_signup, too_many_signups, wording_language
from people.tokens import (connect_url, consent_pk_from_confirm_token, person_from_preferences_token, preferences_token)

PREFILL_KEYS = ("name", "email", "lists", "telegram")
LINK_KEYS = PREFILL_KEYS + ("lang",)


def _client_ip(request):
    """The address our own proxy saw (the last X-Forwarded-For entry), else the socket's."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _initials(name):
    return "".join(word[0] for word in (name or "").split()[:2]).upper() or "?"


def _browser_language(request, codes):
    lowered = {c.lower(): c for c in codes}
    for accepted, _q in parse_accept_lang_header(request.META.get("HTTP_ACCEPT_LANGUAGE", "")):
        if accepted in lowered:
            return lowered[accepted]
        base = accepted.split("-")[0]
        if base in lowered:
            return lowered[base]
    return None


def _render(request, template, context, code, status=200):
    """Render a public page in the reader's language."""
    org = context.get("org") or Organization.get()
    context.update({"org": org, "org_initials": _initials(org.name), "LANGUAGE_CODE": code})
    with translation.override(wording_language(code)):
        return render(request, template, context, status=status)


def _expired(request, status=404):
    languages = list(Language.objects.all())
    codes = [l.code for l in languages]
    code = _browser_language(request, codes) or Language.default_code()
    return _render(request, "public/expired.html", {}, code, status=status)


# --------------------------------------------------------------------------- signup form

def _telegram_bot():
    from channels.models import TelegramBot

    bot = TelegramBot.default()
    return bot if bot is not None and bot.username else None


@xframe_options_sameorigin  # the staff screen shows this page in its preview frame
@require_http_methods(["GET", "POST"])
def join(request):
    org = Organization.get()
    languages = list(Language.objects.all())
    codes = [l.code for l in languages]
    default_code = Language.default_code()

    # The staff preview may try out switches that are not saved yet. Nothing can be submitted from it.
    user = getattr(request, "user", None)
    preview = bool(request.GET.get("preview")) and bool(user and user.is_authenticated and user.has_role("editor"))

    def option(key):
        if preview and f"pv_{key}" in request.GET:
            return request.GET.get(f"pv_{key}") == "1"
        return bool(org.signup_option(f"show_{key}"))

    lists = list(List.objects.filter(kind="campaign"))
    if preview and "pv_lists" in request.GET:
        shown = set(request.GET.get("pv_lists", "").split(","))
        lists = [l for l in lists if l.slug in shown]
    else:
        lists = [l for l in lists if l.on_signup_form]
    bot = _telegram_bot()
    show_first_name = option("first_name")
    show_language = option("language") and len(languages) > 1
    show_telegram = option("telegram") and bot is not None

    # Language: the switch on the form, then the form's own language, then ?lang=, then the browser.
    code = next((c for c in (request.POST.get("switch_lang"), request.POST.get("lang"), request.GET.get("lang")) if c in codes), None)
    code = code or _browser_language(request, codes) or default_code

    # A pre-filled link fills in everything except consent. Consent only ever comes from the person.
    prefilled = any(request.GET.get(k) for k in PREFILL_KEYS)
    link_params = {k: request.GET[k] for k in LINK_KEYS if request.GET.get(k)}
    wording = consent_wording(code, prefilled)
    offered = {l.slug for l in lists}

    posted = request.method == "POST"
    if posted:
        values = {
            "name": request.POST.get("name", "").strip(),
            "email": request.POST.get("email", "").strip(),
            "lists": {s for s in request.POST.getlist("lists") if s in offered},
            "telegram": bool(request.POST.get("telegram")),
        }
    else:
        values = {
            "name": request.GET.get("name", "").strip(),
            "email": request.GET.get("email", "").strip(),
            "lists": {s for s in request.GET.get("lists", "").split(",") if s in offered},
            "telegram": request.GET.get("telegram") == "1",
        }

    errors = {}
    status = 200
    submitting = posted and not request.POST.get("switch_lang") and not preview
    if submitting:
        if request.POST.get("website"):
            # The hidden field was filled in: a bot. Show it the usual thank-you and do nothing.
            return _thanks(request, org, code, telegram_url="")
        try:
            validate_email(values["email"])
        except ValidationError:
            errors["email"] = _wording(code, "email")
        if not values["lists"]:
            errors["lists"] = _wording(code, "lists")
        if not prefilled and not request.POST.get("consent"):
            errors["consent"] = _wording(code, "consent")
        if not errors and too_many_signups(_client_ip(request)):
            errors["form"] = _wording(code, "rate")
            status = 429
        if not errors:
            result = process_signup(
                email=values["email"], first_name=values["name"] if show_first_name else "", language_code=code,
                list_slugs=sorted(values["lists"]), telegram=values["telegram"] and show_telegram, wording=wording,
                ip=_client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", ""), link_params=link_params)
            telegram_url = ""
            if values["telegram"] and show_telegram:
                # Someone who was already here gets a link that connects nothing, so the page looks the same
                # and a stranger cannot attach their own Telegram to another person's address.
                telegram_url = (f"https://t.me/{bot.username}?start={secrets.token_urlsafe(24)}" if result.existing
                                else connect_url(result.person, bot))
            return _thanks(request, org, code, telegram_url=telegram_url)

    query = request.GET.copy()
    context = {
        "org": org, "languages": languages, "code": code, "show_language": show_language,
        "show_first_name": show_first_name, "show_telegram": show_telegram,
        "lists": [{"slug": l.slug, "name": l.display_name(code), "description": l.display_description(code),
                   "checked": l.slug in values["lists"]} for l in lists],
        "values": values, "errors": errors, "prefilled": prefilled, "wording": wording, "preview": preview,
        "action": f"{reverse('public:join')}?{query.urlencode()}" if query else reverse("public:join"),
    }
    return _render(request, "public/join.html", context, code, status=status)


def _wording(code, key):
    with translation.override(wording_language(code)):
        return {
            "email": _("Enter your email address, like name@example.com."),
            "lists": _("Tick at least one thing you would like to hear about."),
            "consent": _("Tick the box to say you agree to receive these messages."),
            "rate": _("Too many signups came from your connection. Please try again in an hour."),
        }[key]


def _thanks(request, org, code, telegram_url):
    return _render(request, "public/thanks.html", {"org": org, "confirm": org.confirm_by_email, "telegram_url": telegram_url}, code)


@require_http_methods(["GET", "POST"])
def confirm(request, token):
    """The button in the confirmation email."""
    pk = consent_pk_from_confirm_token(token)
    consent = ConsentRecord.objects.filter(pk=pk).select_related("person").first() if pk else None
    if consent is None:
        return _expired(request)
    services.confirm_consent(consent)
    person = consent.person
    code = person.language_code
    context = {
        "person": person, "lists": [l.display_name(code) for l in consent.lists.all()],
        "preferences_url": reverse("public:preferences", args=[preferences_token(person)]),
    }
    return _render(request, "public/confirmed.html", context, code)


# --------------------------------------------------------------------------- preferences

def _preferences_context(person, token):
    code = person.language_code
    grid = services.display_grid(person)
    with translation.override(wording_language(code)):
        labels = {"email": _("Email"), "telegram": _("Telegram"), "push": _("Push")}
        by = {"email": _("%(list)s by email"), "telegram": _("%(list)s by Telegram"), "push": _("%(list)s by push")}
        for kind in ("campaign", "app"):
            for row in grid[kind]:
                row["name"] = row["list"].display_name(code)
                row["description"] = row["list"].display_description(code)
                for cell in row["cells"]:
                    cell["label"] = by[cell["channel"]] % {"list": row["name"]}
                    if kind == "app" and cell["channel"] == "push":
                        cell["available"] = False  # push for orders and shifts is controlled inside each app
        grid["column_labels"] = [labels[c] for c in grid["columns"]]
    return {"person": person, "token": token, "grid": grid, "code": code}


def preferences_intro(code):
    """The sentence above the grid. It is also what a person agreed to when they tick a newsletter box there."""
    with translation.override(wording_language(code)):
        return _("Tick how you want to get each kind of message. Changes save as you go.")


def _person_or_none(token):
    return person_from_preferences_token(token)


@require_http_methods(["GET"])
def preferences(request, token):
    person = _person_or_none(token)
    if person is None:
        return _expired(request)
    context = _preferences_context(person, token)
    code = context["code"]
    bot = _telegram_bot()
    connected = person.telegram_links.filter(blocked_at__isnull=True).exists()
    unsubscribed = []
    for public_id in request.GET.get("unsubscribed", "").split(","):
        pk = List.pk_from_public_id(public_id)
        lst = List.objects.filter(pk=pk).first() if pk else None
        if lst:
            unsubscribed.append(lst.display_name(code))
    context.update({
        "languages": list(Language.objects.all()), "intro": preferences_intro(code),
        "done": bool(request.GET.get("done")), "unsubscribed": unsubscribed,
        "stopped": bool(request.GET.get("stopped")),
        # A fresh single-use link each time the page is shown.
        "telegram_url": connect_url(person, bot) if bot is not None and not connected and "telegram" in context["grid"]["columns"] else "",
    })
    return _render(request, "public/preferences.html", context, code)


def _after_change(request, person, token, **flags):
    if request.headers.get("HX-Request") == "true":
        context = _preferences_context(person, token)
        context.update(flags)
        return _render(request, "public/_preferences_grid.html", context, context["code"])
    url = reverse("public:preferences", args=[token])
    return redirect(f"{url}?{urlencode(flags)}" if flags else url)


@require_POST
def preferences_toggle(request, token):
    """One tick box. A person may switch their own newsletters back on; that is their consent."""
    person = _person_or_none(token)
    if person is None:
        return _expired(request)
    lst = List.objects.filter(pk=List.pk_from_public_id(request.POST.get("list", ""))).first()
    channel = request.POST.get("channel", "")
    if lst is None or channel not in CHANNELS:
        return HttpResponseBadRequest("Unknown list or channel")
    grid = _preferences_context(person, token)["grid"]
    row = next((r for kind in ("campaign", "app") for r in grid[kind] if r["list"].pk == lst.pk), None)
    cell = next((c for c in row["cells"] if c["channel"] == channel), None) if row else None
    if cell is None or not cell["available"]:
        return HttpResponseBadRequest("This cannot be changed here")
    if request.POST.get("on") == "1":
        consent = None
        if lst.kind == "campaign":
            code = person.language_code
            wording = f"{preferences_intro(code)} {cell['label']}"
            consent = services.record_consent(
                person, [lst], "signup_form", wording, language=code, ip=_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""), link_params={"source": "preferences"},
                confirmed=True)  # they came here through a link we emailed or messaged them
        services.set_subscription(person, lst, channel, True, consent=consent)
    else:
        services.set_subscription(person, lst, channel, False)
    return _after_change(request, person, token)


@require_POST
def preferences_language(request, token):
    person = _person_or_none(token)
    if person is None:
        return _expired(request)
    language = Language.objects.filter(code=request.POST.get("lang", "")).first()
    if language and language.pk != person.language_id:
        person.language = language
        person.save(update_fields=["language", "updated_at"])
    return redirect("public:preferences", token=token)


@require_POST
def preferences_unsubscribe_all(request, token):
    person = _person_or_none(token)
    if person is None:
        return _expired(request)
    services.unsubscribe_all_news(person)
    return _after_change(request, person, token, stopped=1)


# --------------------------------------------------------------------------- one-click unsubscribe

@csrf_exempt  # mail apps post here on the reader's behalf (RFC 8058); the token is the proof
@require_http_methods(["GET", "POST", "HEAD"])
def unsubscribe(request, token):
    """Turn off, for email only, the list that let this message through; then show the preferences page."""
    from delivery.models import Message

    message = Message.objects.filter(token=token).select_related("person", "list", "campaign").first()
    if message is None or message.person is None:
        return _expired(request)
    if request.method == "HEAD":
        return HttpResponse()
    person = message.person
    if message.list_id:
        lists = [message.list]
    elif message.campaign_id:
        lists = list(message.campaign.lists.filter(kind="campaign"))
    else:
        lists = []
    for lst in lists:
        services.set_subscription(person, lst, "email", False)
    if request.method == "POST" and request.POST.get("List-Unsubscribe") == "One-Click":
        return HttpResponse("Unsubscribed", content_type="text/plain")
    url = reverse("public:preferences", args=[preferences_token(person)])
    return redirect(f"{url}?unsubscribed={','.join(l.public_id for l in lists)}" if lists else url)
