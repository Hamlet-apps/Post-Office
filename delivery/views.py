"""The Outbox (spec section 13) and the two tracking routes (section 7)."""
import base64
import datetime

from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Q, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import formats, timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from campaigns.models import Campaign
from core.models import Building, Organization
from core.permissions import role_required
from people.models import CHANNELS

from . import limits, pipeline
from .models import LinkClick, Message, TrackedLink

PER_PAGE = 25
EXPAND_PER_PAGE = 50
STATUS_STATES = {
    "waiting": ("queued", "sending"),
    "delivered": Message.DELIVERED_STATES,
    "failed": ("failed",),
    "skipped": ("skipped",),
}


# --------------------------------------------------------------------------- filters

def _parse_day(value):
    try:
        return datetime.date.fromisoformat(value or "")
    except ValueError:
        return None


def _filters(request):
    get = request.GET
    channel = get.get("channel", "")
    status = get.get("status", "")
    return {
        "source": get.get("source", ""),
        "channel": channel if channel in CHANNELS else "",
        "status": status if status in (*STATUS_STATES, "scheduled") else "",
        "start": _parse_day(get.get("from")),
        "end": _parse_day(get.get("to")),
        "q": (get.get("q") or "").strip(),
        # ?campaign=cmp_12 lists that campaign's messages one by one (linked from its report).
        "campaign": Campaign.pk_from_public_id(get.get("campaign", "")) or "",
    }


def _day_bounds(f, org):
    start = end = None
    if f["start"]:
        start = datetime.datetime.combine(f["start"], datetime.time.min, tzinfo=org.tz)
    if f["end"]:
        end = datetime.datetime.combine(f["end"] + datetime.timedelta(days=1), datetime.time.min, tzinfo=org.tz)
    return start, end


def _message_q(f, org, with_dates=True):
    q = Q()
    if f["channel"]:
        q &= Q(channel=f["channel"])
    if f["status"] in STATUS_STATES:
        q &= Q(state__in=STATUS_STATES[f["status"]])
    if with_dates:
        start, end = _day_bounds(f, org)
        if start:
            q &= Q(created_at__gte=start)
        if end:
            q &= Q(created_at__lt=end)
    for word in f["q"].split()[:6]:
        q &= (Q(person__first_name__icontains=word) | Q(person__last_name__icontains=word)
              | Q(person__email__icontains=word) | Q(subject_snapshot__icontains=word)
              | Q(template__name__icontains=word) | Q(campaign__name__icontains=word))
    return q


def _rows_queryset(f, org):
    """(kind, id, time) for every Outbox row: "c" a campaign summary, "m" one message."""
    source = f["source"]
    mq = _message_q(f, org)
    if f.get("campaign"):
        m = (Message.objects.filter(campaign_id=f["campaign"]).filter(mq).order_by()
             .annotate(kind=Value("m"), at=F("created_at")).values_list("kind", "pk", "at"))
        return m.order_by("-at", "-id")
    messages = Message.objects.none()
    if f["status"] != "scheduled" and source != "campaigns":
        messages = Message.objects.filter(campaign__isnull=True).filter(mq)
        if source:
            pk = Building.pk_from_public_id(source)
            messages = messages.filter(building_id=pk) if pk else messages.none()
    campaigns = Campaign.objects.exclude(state="draft").annotate(at=Coalesce("started_at", "scheduled_at", "created_at"))
    if source not in ("", "campaigns"):
        campaigns = campaigns.none()
    else:
        has_messages = Exists(Message.objects.filter(campaign=OuterRef("pk")).filter(_message_q(f, org, with_dates=False)))
        not_started = Q(started_at__isnull=True, state__in=("scheduled", "paused"))
        for word in f["q"].split()[:6]:
            not_started &= Q(name__icontains=word)
        if f["status"] == "scheduled":
            campaigns = campaigns.filter(not_started)
        elif f["status"] or f["channel"]:
            campaigns = campaigns.filter(has_messages)
        else:
            campaigns = campaigns.filter(has_messages | not_started)
        start, end = _day_bounds(f, org)
        if start:
            campaigns = campaigns.filter(at__gte=start)
        if end:
            campaigns = campaigns.filter(at__lt=end)
    c = campaigns.order_by().annotate(kind=Value("c")).values_list("kind", "pk", "at")
    m = messages.order_by().annotate(kind=Value("m"), at=F("created_at")).values_list("kind", "pk", "at")
    return c.union(m, all=True).order_by("-at", "-id")


# --------------------------------------------------------------------------- presentation

def _when(moment, org, today=None):
    if moment is None:
        return ""
    local = moment.astimezone(org.tz)
    today = today or timezone.now().astimezone(org.tz).date()
    if local.date() == today:
        return formats.date_format(local, "g:i A")
    if local.year == today.year:
        return formats.date_format(local, "M j, g:i A")
    return formats.date_format(local, "M j, Y")


def _decorate_message(message, org, today=None):
    """Attach what the row template shows."""
    message.when = _when(message.sent_at or message.created_at, org, today)
    message.href = ""
    if message.campaign_id:
        message.href = f"/campaigns/{message.campaign.public_id}/"
        message.kind_label = _("Campaign")
        message.heading = message.campaign.name
        message.subline = message.subject_snapshot if message.subject_snapshot != message.campaign.name else ""
    elif message.template_id:
        message.href = f"/templates/{message.template.key}/"
        message.kind_label = message.building.name if message.building_id else _("App message")
        message.heading = message.template.name
        message.subline = message.subject_snapshot if message.subject_snapshot != message.template.name else ""
    elif message.quick_message_id:
        message.kind_label = _("Quick message")
        message.heading = _("Quick message")
        message.subline = str(message.quick_message)[:80]
    else:
        message.kind_label = _("Post Office")
        message.heading = message.subject_snapshot or _("Message")
        message.subline = ""
    message.waiting_until = ""
    if message.state == "queued" and message.send_after and message.send_after > timezone.now():
        message.waiting_until = _when(message.send_after, org, today)
    return message


def _message_rows(queryset, org):
    today = timezone.now().astimezone(org.tz).date()
    rows = queryset.select_related("person", "campaign", "template", "building", "quick_message")
    return [_decorate_message(m, org, today) for m in rows]


def _campaign_rows(ids, org):
    campaigns = {c.pk: c for c in Campaign.objects.filter(pk__in=ids).prefetch_related("lists", "push_apps")}
    stats = {row["campaign_id"]: row for row in (
        Message.objects.filter(campaign_id__in=ids).exclude(state="skipped").values("campaign_id")
        .annotate(people=Count("person_id", distinct=True), failed=Count("id", filter=Q(state="failed")),
                  waiting=Count("id", filter=Q(state__in=("queued", "sending")))))}
    languages = {}
    for row in (Message.objects.filter(campaign_id__in=ids).exclude(state="skipped")
                .values("campaign_id", "language").annotate(fallback=Count("id", filter=Q(used_fallback=True)))
                .order_by("language")):
        languages.setdefault(row["campaign_id"], []).append({"code": row["language"], "fallback": bool(row["fallback"])})
    today = timezone.now().astimezone(org.tz).date()
    for campaign in campaigns.values():
        stat = stats.get(campaign.pk)
        lists = list(campaign.lists.all())
        campaign.list_names = ", ".join(lst.display_name() for lst in lists) or _("No lists")
        campaign.not_started = stat is None and campaign.started_at is None
        if stat is not None:
            campaign.people_count = stat["people"]
        elif campaign.not_started:
            campaign.people_count = pipeline.audience(lists, campaign.channels, campaign.sender, campaign.bot,
                                                      campaign.push_apps.all())["people"]
        else:
            campaign.people_count = (campaign.totals or {}).get("people", 0)
        campaign.failed_count = stat["failed"] if stat else 0
        campaign.waiting_count = stat["waiting"] if stat else 0
        campaign.can_expand = stat is not None
        campaign.language_tags = languages.get(campaign.pk, [])
        campaign.channel_list = [c for c in CHANNELS if c in (campaign.channels or [])]
        campaign.when = _when(campaign.started_at or campaign.scheduled_at or campaign.created_at, org, today)
        campaign.href = (f"/campaigns/{campaign.public_id}/send/" if campaign.state == "scheduled"
                         else f"/campaigns/{campaign.public_id}/")
    return campaigns


def _counters(org):
    now = timezone.now()
    day = limits.local_day_start(org, now)
    waiting = Message.objects.filter(state__in=("queued", "sending")).count()
    held = Message.objects.filter(state="queued", send_after__gt=now).count()
    delivered = Message.objects.filter(state__in=Message.DELIVERED_STATES, sent_at__gte=day).count()
    failed_rows = list(Message.objects.filter(state="failed", updated_at__gte=day).values("channel").annotate(n=Count("id")))
    failed = sum(row["n"] for row in failed_rows)
    scheduled = Campaign.objects.filter(state="scheduled")
    nxt = scheduled.exclude(scheduled_at__isnull=True).order_by("scheduled_at").first()

    if not waiting:
        waiting_note = _("Nothing queued right now")
    elif held:
        waiting_note = ngettext("%(n)s is held until later", "%(n)s are held until later", held) % {"n": f"{held:,}"}
    else:
        waiting_note = _("Going out now")
    channel_names = {"email": _("email"), "telegram": _("Telegram"), "push": _("notifications")}
    if not failed:
        failed_note = _("Nothing failed today")
    elif len(failed_rows) == 1:
        failed_note = _("All on %(channel)s") % {"channel": channel_names.get(failed_rows[0]["channel"], failed_rows[0]["channel"])}
    else:
        failed_note = ", ".join(f"{row['n']:,} {channel_names.get(row['channel'], row['channel'])}" for row in failed_rows)
    return [
        {"label": _("Waiting to send"), "icon": "clock", "value": waiting, "note": waiting_note},
        {"label": _("Delivered today"), "icon": "check", "value": delivered,
         "note": _("Out of %(n)s") % {"n": f"{delivered + failed:,}"}},
        {"label": _("Failed today"), "icon": "warning", "value": failed, "note": failed_note},
        {"label": _("Scheduled"), "icon": "calendar", "value": scheduled.count(),
         "note": (f"{nxt.name}, {_when(nxt.scheduled_at, org)}" if nxt else _("No campaign is scheduled"))},
    ]


# --------------------------------------------------------------------------- screens

@role_required("viewer")
def outbox(request):
    org = Organization.get()
    f = _filters(request)
    page = Paginator(_rows_queryset(f, org), PER_PAGE).get_page(request.GET.get("page"))
    keys = list(page.object_list)
    campaigns = _campaign_rows([pk for kind, pk, _at in keys if kind == "c"], org)
    messages = {m.pk: m for m in _message_rows(Message.objects.filter(pk__in=[pk for kind, pk, _at in keys if kind == "m"]), org)}
    rows = []
    for kind, pk, _at in keys:
        item = campaigns.get(pk) if kind == "c" else messages.get(pk)
        if item is not None:
            rows.append({"kind": kind, "item": item})
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "delivery/outbox.html", {
        "f": f, "rows": rows, "page": page, "counters": _counters(org),
        "buildings": list(Building.objects.all()),
        "channel_choices": [("email", _("Email")), ("telegram", _("Telegram")), ("push", _("Notifications"))],
        "status_choices": [("waiting", _("Waiting")), ("delivered", _("Delivered")), ("failed", _("Failed")),
                           ("skipped", _("Skipped")), ("scheduled", _("Scheduled"))],
        "filter_query": params.urlencode(),
        "is_filtered": any(f.values()),
        "page_numbers": page.paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1),
    })


@role_required("viewer")
@require_GET
def campaign_messages(request, public_id):
    """The rows behind one campaign's summary row (HTMX)."""
    org = Organization.get()
    campaign = Campaign.get_by_public_id(public_id)
    f = _filters(request)
    queryset = Message.objects.filter(campaign=campaign).filter(_message_q(f, org, with_dates=False))
    page = Paginator(queryset, EXPAND_PER_PAGE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "delivery/_campaign_messages.html", {
        "campaign": campaign, "page": page, "messages_page": _message_rows(Message.objects.filter(
            pk__in=[m.pk for m in page.object_list]), org),
        "filter_query": params.urlencode(),
    })


@role_required("editor")
@require_POST
def retry(request, public_id):
    """"Try again" on a failed row."""
    message = Message.get_by_public_id(public_id)
    message = pipeline.retry_message(message)
    org = Organization.get()
    if request.headers.get("HX-Request"):
        row = _message_rows(Message.objects.filter(pk=message.pk), org)[0]
        response = render(request, "delivery/_message_row.html",
                          {"m": row, "sub": request.POST.get("sub") == "1"})
        note = (_("Sent") if row.is_delivered else _("Still not delivered") if row.state == "failed" else _("Queued again"))
        response["HX-Trigger"] = '{"po-toast": "%s"}' % note
        return response
    return redirect(request.META.get("HTTP_REFERER") or "delivery:outbox")


# --------------------------------------------------------------------------- tracking (no sign-in)

_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _mark_opened(message, now):
    fields = {}
    if message.opened_at is None:
        fields["opened_at"] = now
    if message.state == "sent":
        fields["state"] = "opened"
    if fields:
        Message.objects.filter(pk=message.pk).update(updated_at=now, **fields)


@never_cache
def open_pixel(request, token):
    """/t/o/<message token>.gif: the 1x1 image in newsletter emails."""
    message = Message.objects.filter(token=token).first()
    if message is not None and message.state in Message.DELIVERED_STATES:
        _mark_opened(message, timezone.now())
    return HttpResponse(_PIXEL, content_type="image/gif")


@never_cache
def click(request, token):
    """/t/c/<link token>?m=<message token>: record the click and go to the real address."""
    link = TrackedLink.objects.filter(token=token).first()
    if link is None:
        raise Http404("Unknown link")
    message_token = request.GET.get("m", "")
    message = Message.objects.filter(token=message_token, campaign_id=link.campaign_id).first() if message_token else None
    if message is not None:
        now = timezone.now()
        LinkClick.objects.create(message=message, link=link)
        fields = {"state": "clicked"} if message.state in Message.DELIVERED_STATES else {}
        if message.clicked_at is None:
            fields["clicked_at"] = now
        if message.opened_at is None:
            fields["opened_at"] = now
        if fields:
            Message.objects.filter(pk=message.pk).update(updated_at=now, **fields)
    response = HttpResponseRedirect(link.url)
    return response
