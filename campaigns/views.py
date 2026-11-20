"""Campaigns: the list, step 2 (audience and send), the report and the actions on a campaign."""
import csv
import json
from datetime import datetime, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import dateformat, timezone
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from channels.models import EmailSender, PushApp, TelegramBot
from content.models import Content, Template
from core.models import Language, Organization
from core.permissions import role_required
from delivery.models import Message
from people.models import List, Segment, Subscription

from . import checks, reports, services
from .models import Campaign

PAGE_SIZE = 20
# The filter tabs: key, label, the states it shows.
TABS = [
    ("all", "All", None),
    ("draft", "Drafts", ("draft",)),
    ("scheduled", "Scheduled", ("scheduled",)),
    ("sending", "Sending", ("sending", "paused")),
    ("sent", "Sent", ("sent", "cancelled")),
]


def _get(public_id):
    return Campaign.get_by_public_id(public_id)


def _back(request, fallback):
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(fallback)


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


# --------------------------------------------------------------------------- list

@role_required("viewer")
def list_view(request):
    q = request.GET.get("q", "").strip()
    state = request.GET.get("state", "all")
    if state not in {key for key, _label, _states in TABS}:
        state = "all"
    base = Campaign.objects.all()
    if q:
        base = base.filter(name__icontains=q)
    counts = dict(base.values_list("state").annotate(n=Count("id")))
    tabs = []
    for key, label, states in TABS:
        n = sum(counts.values()) if states is None else sum(counts.get(s, 0) for s in states)
        if key == "sending" and not n and state != "sending":
            continue  # only shown while something is going out
        tabs.append({"key": key, "label": _(label), "count": n, "current": key == state})
    qs = base
    states = dict((key, states) for key, _label, states in TABS)[state]
    if states:
        qs = qs.filter(state__in=states)
    page = Paginator(qs.select_related("sender", "bot"), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "campaigns/list.html", {
        "q": q, "state": state, "tabs": tabs, "page": page,
        "rows": reports.campaign_rows(page.object_list),
        "total": sum(counts.values()),
        "any_campaigns": bool(q) or Campaign.objects.exists(),
        "campaign_templates": Template.objects.filter(kind="campaign").order_by("name") if request.user.can_edit else [],
    })


# --------------------------------------------------------------------------- create

@role_required("editor")
def new(request):
    """Create a draft and open the design step. A plain link (GET) works too."""
    key = (request.POST.get("template") or request.GET.get("template") or "").strip()
    template = Template.objects.filter(key=key, kind="campaign").first() if key else None
    campaign = Campaign.objects.create(
        name=(request.POST.get("name") or "").strip()[:200] or _("Untitled campaign"),
        sender=EmailSender.default_newsletter(),
        bot=TelegramBot.default(),
        channels=["email"],
        created_by=request.user,
    )
    if template is not None:
        services.copy_content(template, campaign)
    return redirect("campaigns:design", public_id=campaign.public_id)


@role_required("editor")
def design(request, public_id):
    """Step 1 belongs to the editor (content.editor); this only routes to it."""
    try:
        from content.editor import campaign_design
    except ImportError:
        campaign = _get(public_id)
        return render(request, "campaigns/design_missing.html", {"campaign": campaign}, status=503)
    return campaign_design(request, public_id)


# --------------------------------------------------------------------------- step 2: audience and send

def _list_counts():
    """{list id: people with a subscription that is on}."""
    rows = Subscription.objects.filter(state="on", list__kind="campaign").values("list_id").annotate(n=Count("person_id", distinct=True))
    return {row["list_id"]: row["n"] for row in rows}


def _send_context(request, campaign, check_links=True):
    org = Organization.get()
    chosen_lists = set(campaign.lists.values_list("pk", flat=True))
    chosen_apps = set(campaign.push_apps.values_list("pk", flat=True))
    channels = campaign.channels or []
    counts = _list_counts()
    default_code = Language.default_code()
    lists = [
        {"list": lst, "name": lst.display_name(default_code), "count": counts.get(lst.pk, 0), "on": lst.pk in chosen_lists}
        for lst in List.objects.filter(kind="campaign")
    ]
    audience = checks.audience(campaign)
    # Counts for the channel tiles show what each channel could reach, chosen or not.
    reach = checks.audience(campaign, channels=["email", "telegram", "push"], bot=campaign.bot or TelegramBot.default())
    member_counts = dict(PushApp.objects.annotate(n=Count("members")).values_list("pk", "n"))
    push_apps = [
        {"app": app, "count": member_counts.get(app.pk, 0), "on": app.pk in chosen_apps}
        for app in PushApp.objects.select_related("building")
    ]
    checklist = checks.checklist(campaign, links=check_links, by_language=audience["by_language"])
    blockers = [c for c in checklist if not c.ok and c.blocking]
    rows = checks.language_rows(campaign, audience["by_language"])
    missing = [r for r in rows if not r["complete"]]
    forms = checks.needed_forms(campaign)

    local_now = org.local_now()
    when = campaign.scheduled_at.astimezone(org.tz) if campaign.scheduled_at else (local_now + timedelta(days=1)).replace(hour=9, minute=0)
    no_email = audience["people"] and "email" in channels and audience["people"] - audience["email"]
    return {
        "campaign": campaign,
        "editable": campaign.state == "draft" and request.user.can_edit,
        "lists": lists,
        "segments": Segment.objects.all(),
        "senders": EmailSender.objects.filter(purpose="newsletter"),
        "bots": TelegramBot.objects.all(),
        "push_apps": push_apps,
        "email_on": "email" in channels,
        "telegram_on": "telegram" in channels,
        "audience": audience,
        "reach": reach,
        "no_email": no_email if no_email and no_email > 0 else 0,
        "checklist": checklist,
        "blockers": blockers,
        "can_send": not blockers,
        "language_rows": rows,
        "missing_languages": missing,
        "show_email": "email" in forms,
        "show_notification": "notification" in forms,
        "notification_label": _("Telegram") if "telegram" in channels or "push" not in channels else _("Push"),
        "when_date": when.strftime("%Y-%m-%d"),
        "when_time": when.strftime("%H:%M"),
        "today": local_now.strftime("%Y-%m-%d"),
        # Formatted here: the template date filter would shift them to the server's time zone.
        "scheduled_text": dateformat.format(campaign.scheduled_at.astimezone(org.tz), "M j, g:i A") if campaign.scheduled_at else "",
        "scheduled_day": dateformat.format(campaign.scheduled_at.astimezone(org.tz), "l, F j") if campaign.scheduled_at else "",
        "scheduled_time": dateformat.format(campaign.scheduled_at.astimezone(org.tz), "g:i A") if campaign.scheduled_at else "",
        "tz_name": org.timezone,
        "test_email": request.user.email,
        "written_email": [r["language"] for r in rows if r["email"]],
    }


@role_required("editor")
def send(request, public_id):
    campaign = _get(public_id)
    if campaign.state not in ("draft", "scheduled"):
        return redirect("campaigns:report", public_id=campaign.public_id)
    return render(request, "campaigns/send.html", _send_context(request, campaign))


@role_required("editor")
@require_POST
def audience(request, public_id):
    """Autosave of the "Who gets it" choices. Answers with the refreshed panels."""
    campaign = _get(public_id)
    if campaign.state != "draft":
        return HttpResponseBadRequest(_("Move the campaign back to a draft to change it."))
    list_ids = [List.pk_from_public_id(v) for v in request.POST.getlist("lists")]
    campaign.lists.set(List.objects.filter(kind="campaign", pk__in=[i for i in list_ids if i]))
    app_ids = [PushApp.pk_from_public_id(v) for v in request.POST.getlist("push_apps")]
    apps = list(PushApp.objects.filter(pk__in=[i for i in app_ids if i]))
    campaign.push_apps.set(apps)
    picked = request.POST.getlist("channels")
    channels = [c for c in ("email", "telegram") if c in picked]
    if apps:
        channels.append("push")
    campaign.channels = channels
    sender_pk = EmailSender.pk_from_public_id(request.POST.get("sender", ""))
    campaign.sender = EmailSender.objects.filter(pk=sender_pk, purpose="newsletter").first() if sender_pk else None
    bot_pk = TelegramBot.pk_from_public_id(request.POST.get("bot", ""))
    campaign.bot = TelegramBot.objects.filter(pk=bot_pk).first() if bot_pk else None
    campaign.save(update_fields=["channels", "sender", "bot", "updated_at"])
    if not _is_htmx(request):
        return redirect("campaigns:send", public_id=campaign.public_id)
    context = _send_context(request, campaign)
    context["oob"] = True
    return render(request, "campaigns/_send_live.html", context)


def _parse_when(request, org):
    """The chosen moment as an aware datetime, or (None, error)."""
    try:
        naive = datetime.strptime(f"{request.POST.get('date', '')} {request.POST.get('time', '')}".strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None, _("Pick a day and a time for the send.")
    at = naive.replace(tzinfo=org.tz)
    if at <= timezone.now():
        return None, _("That time has already passed. Pick a later one, or send now.")
    return at, ""


@role_required("editor")
@require_POST
def schedule(request, public_id):
    """Send now or schedule. The checklist is enforced here, not only on the screen."""
    campaign = _get(public_id)
    org = Organization.get()
    back = redirect("campaigns:send", public_id=campaign.public_id)
    if campaign.state != "draft":
        messages.error(request, _("This campaign is not a draft any more."))
        return back
    ok, reasons = checks.can_schedule(campaign)
    if not ok:
        messages.error(request, _("This campaign cannot be sent yet: %(reasons)s") % {"reasons": "; ".join(str(r) for r in reasons)})
        return back
    spread = 60 if request.POST.get("spread") else 0
    if request.POST.get("when") == "now":
        try:
            services.send_now(campaign, spread)
        except ImportError:
            services.cancel(campaign)
            messages.error(request, _("Sending is not set up on this install yet. The campaign is still a draft."))
            return back
        except Exception:
            campaign.refresh_from_db()
            messages.error(request, _("The send could not start just now. It will be tried again within a minute."))
            return redirect("campaigns:send", public_id=campaign.public_id)
        messages.success(request, _("The campaign is going out."))
        return redirect("campaigns:report", public_id=campaign.public_id)
    at, error = _parse_when(request, org)
    if at is None:
        messages.error(request, error)
        return back
    services.schedule(campaign, at, spread)
    messages.success(request, _("Scheduled for %(when)s.") % {"when": dateformat.format(at.astimezone(org.tz), "M j, g:i A")})
    return back


@role_required("editor")
@require_POST
def test(request, public_id):
    """Send a test email to one address, in one language or one message per written language."""
    from content.render import build_context, render_email

    campaign = _get(public_id)
    org = Organization.get()

    def answer(ok, text):
        if _is_htmx(request):
            return HttpResponse(format_html(
                '<div class="po-notice {}" role="status">{}</div>', "po-notice--ok" if ok else "po-notice--danger", text))
        (messages.success if ok else messages.error)(request, text)
        return redirect("campaigns:send", public_id=campaign.public_id)

    to_email = (request.POST.get("email") or "").strip()
    if "@" not in to_email or " " in to_email:
        return answer(False, _("Enter the address to send the test to."))
    if campaign.sender is None:
        return answer(False, _("Choose an email sender first."))
    wanted = request.POST.get("language") or "all"
    contents = [c for c in Content.objects.filter(campaign=campaign, form="email") if c.is_written]
    order = Language.codes()
    contents.sort(key=lambda c: order.index(c.language) if c.language in order else 99)
    if wanted != "all":
        contents = [c for c in contents if c.language == wanted]
    if not contents:
        return answer(False, _("There is no written email to test in that language yet."))
    try:
        from delivery import pipeline

        send_test_email = pipeline.send_test_email
    except (ImportError, AttributeError):
        return answer(False, _("Sending is not set up on this install yet."))
    context = build_context(None, {}, {"unsubscribe": "#", "preferences": "#"})
    name = request.user.first_name
    context.update({"first_name": name, "last_name": ""})
    context["person"].update({"first_name": name, "email": to_email})
    sent, errors = 0, []
    for content in contents:
        rendered = render_email(content, dict(context), message_type="newsletter", org=org)
        rendered.subject = f"[{_('Test')}] {rendered.subject}"
        ok, error = send_test_email(campaign.sender, to_email, rendered)
        if ok:
            sent += 1
        else:
            errors.append(str(error or _("The mail server refused it.")))
    if errors:
        return answer(False, _("The test did not go out: %(reason)s") % {"reason": errors[0]})
    return answer(True, ngettext(
        "Sent %(n)s test message to %(email)s.", "Sent %(n)s test messages to %(email)s, one for each language.", sent,
    ) % {"n": sent, "email": to_email})


# --------------------------------------------------------------------------- actions

@role_required("editor")
@require_POST
def pause(request, public_id):
    campaign = _get(public_id)
    if campaign.state == "sending":
        services.pause(campaign)
        messages.success(request, _("Paused. Messages that have not gone out yet are waiting."))
    return redirect("campaigns:report", public_id=campaign.public_id)


@role_required("editor")
@require_POST
def resume(request, public_id):
    campaign = _get(public_id)
    if campaign.state == "paused":
        services.resume(campaign)
        messages.success(request, _("Sending again from where it stopped."))
    return redirect("campaigns:report", public_id=campaign.public_id)


@role_required("editor")
@require_POST
def cancel(request, public_id):
    campaign = _get(public_id)
    if campaign.state == "scheduled":
        services.cancel(campaign)
        messages.success(request, _("Back to a draft. Nothing was sent."))
        return redirect("campaigns:send", public_id=campaign.public_id)
    if campaign.state in ("sending", "paused"):
        services.cancel(campaign)
        messages.success(request, _("Stopped. Messages that had not gone out will not be sent."))
    return redirect("campaigns:report", public_id=campaign.public_id)


@role_required("editor")
@require_POST
def duplicate(request, public_id):
    campaign = _get(public_id)
    copy = services.duplicate(campaign, request.user, name=_("%(name)s (copy)") % {"name": campaign.name})
    messages.success(request, _("Here is a new draft with the same content."))
    return redirect("campaigns:design", public_id=copy.public_id)


@role_required("editor")
@require_POST
def delete(request, public_id):
    campaign = _get(public_id)
    if campaign.state != "draft":
        messages.error(request, _("Only drafts can be deleted."))
        return redirect("campaigns:list")
    campaign.delete()
    messages.success(request, _("Draft deleted."))
    return redirect("campaigns:list")


@role_required("editor")
@require_POST
def rename(request, public_id):
    campaign = _get(public_id)
    name = (request.POST.get("name") or "").strip()[:200]
    if name:
        campaign.name = name
        campaign.save(update_fields=["name", "updated_at"])
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({"po-toast": _("Saved")})
        return response
    return _back(request, reverse("campaigns:list"))


@role_required("editor")
@require_POST
def save_template(request, public_id):
    campaign = _get(public_id)
    name = (request.POST.get("name") or "").strip()[:160] or campaign.name
    try:
        from content.services import save_as_campaign_template

        save_as_campaign_template(campaign, name)
    except ImportError:
        messages.error(request, _("Templates are not set up on this install yet."))
    else:
        messages.success(request, _("Saved as the template \"%(name)s\". Pick it when you start a new campaign.") % {"name": name})
    return _back(request, reverse("campaigns:list"))


# --------------------------------------------------------------------------- report

@role_required("viewer")
def report(request, public_id):
    campaign = _get(public_id)
    if campaign.state == "draft":
        return redirect("campaigns:design", public_id=campaign.public_id)
    if campaign.state == "scheduled":
        return redirect("campaigns:send", public_id=campaign.public_id)
    org = Organization.get()
    data = reports.campaign_report(campaign)
    default_code = Language.default_code()
    words = {"email": _("email"), "telegram": _("Telegram"), "push": _("push")}
    used = [row["channel"] for row in data["channels"]] or [c for c in campaign.channels or [] if c in words]
    channel_names = [str(words[c]) for c in used]
    started_local = data["started_at"].astimezone(org.tz) if data["started_at"] else None
    waiting = Message.objects.filter(campaign=campaign, state__in=("queued", "sending")).count() if campaign.state in ("sending", "paused") else 0
    return render(request, "campaigns/report.html", {
        "campaign": campaign,
        "report": data,
        "stats": data["stats"],
        "started_day": dateformat.format(started_local, "M j") if started_local else "",
        "started_time": dateformat.format(started_local, "g:i A") if started_local else "",
        "list_names": ", ".join(l.display_name(default_code) for l in campaign.lists.all()),
        "channel_names": _join_words(channel_names),
        "waiting": waiting,
        "state_tag": reports.STATE_TAGS.get(campaign.state, ""),
    })


def _join_words(words):
    words = [w for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return _("%(first)s and %(last)s") % {"first": ", ".join(words[:-1]), "last": words[-1]}


@role_required("viewer")
def export(request, public_id):
    """The report's message rows as a spreadsheet file."""
    campaign = _get(public_id)
    org = Organization.get()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{campaign.public_id}-report.csv"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow([_("Person"), _("Email"), _("Channel"), _("Language"), _("Status"), _("Sent"), _("Opened"), _("Clicked"), _("Reason")])

    def local(at):
        return at.astimezone(org.tz).strftime("%Y-%m-%d %H:%M") if at else ""

    def safe(text):
        text = str(text or "")
        return "'" + text if text[:1] in ("=", "+", "-", "@") else text  # keep spreadsheets from running it as a formula

    rows = Message.objects.filter(campaign=campaign).select_related("person").order_by("id")
    for m in rows.iterator(chunk_size=2000):
        person = m.person
        writer.writerow([
            safe(person.display_name if person else ""), safe(person.email if person else ""), m.channel, m.language,
            m.get_state_display(), local(m.sent_at), local(m.opened_at), local(m.clicked_at), safe(m.error),
        ])
    return response
