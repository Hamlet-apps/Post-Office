"""The dashboard at / (spec section 13, mockup Main).

Everything is counted in the organization's time zone. Charts are inline SVG
drawn by core/charts.py; the 7, 30 and 90 day switch reloads the Engagement
panel with HTMX (GET /?range=7 answers with just that panel).
"""
from datetime import datetime, time, timedelta

from django.db.models import Count, Max, Min, Q
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import dateformat, timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from core import charts
from core.models import Building, Language, Organization
from core.permissions import role_required

RANGES = [(7, "7 days"), (30, "30 days"), (90, "90 days")]
CHANNEL_WORDS = {"email": "Email", "telegram": "Telegram", "push": "Push"}
LANGUAGE_COLOURS = ["#0B7A85", "#EE8A5A", "#FFD2B5", "#4FA7B0", "#94461A", "#5B6170", "#1A6B44", "#C9C4BB"]


def _delivered():
    from delivery.models import Message

    return Q(state__in=Message.DELIVERED_STATES)


OPENED = Q(opened_at__isnull=False) | Q(state__in=("opened", "clicked"))
CLICKED = Q(clicked_at__isnull=False) | Q(state="clicked")


def _midnight(org, day):
    return datetime.combine(day, time.min).replace(tzinfo=org.tz)


def _join(words):
    words = [str(w) for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return _("%(first)s and %(last)s") % {"first": ", ".join(words[:-1]), "last": words[-1]}


def _channel_words(channels):
    ordered = [c for c in ("email", "telegram", "push") if c in channels]
    words = [str(_(CHANNEL_WORDS[c])) for c in ordered]
    return _join([words[0]] + [w.lower() if w != "Telegram" else w for w in words[1:]]) if words else ""


def _first_subscriptions(since):
    """(person id, moment) for everyone whose first newsletter subscription that is on began after `since`."""
    from people.models import Subscription

    return list(
        Subscription.objects.filter(list__kind="campaign", state="on")
        .values("person_id").annotate(first=Min("created_at")).filter(first__gte=since)
        .values_list("person_id", "first")
    )


# --------------------------------------------------------------------------- header

def greeting(org, user):
    hour = org.local_now().hour
    if hour < 12:
        text = _("Good morning, %(name)s")
    elif hour < 18:
        text = _("Good afternoon, %(name)s")
    else:
        text = _("Good evening, %(name)s")
    return text % {"name": user.first_name}


def status_line():
    from campaigns.models import Campaign
    from delivery.models import Message
    from people.models import Person

    states = dict(Campaign.objects.values_list("state").annotate(n=Count("id")))
    waiting = Message.objects.filter(state__in=("queued", "sending")).count()
    if not states and not waiting and not Person.objects.exists():
        return _("Nothing has been sent yet. Add people, then write your first campaign.")
    scheduled = states.get("scheduled", 0)
    if states.get("sending"):
        first = _("A campaign is going out now")
    elif scheduled == 0:
        first = _("No campaign is scheduled")
    elif scheduled == 1:
        first = _("One campaign is scheduled")
    else:
        first = _("%(n)s campaigns are scheduled") % {"n": scheduled}
    if waiting:
        second = ngettext("%(n)s message is waiting in the outbox", "%(n)s messages are waiting in the outbox", waiting) % {"n": f"{waiting:,}"}
    else:
        second = _("the outbox is clear")
    return _("%(first)s and %(second)s.") % {"first": first, "second": second}


# --------------------------------------------------------------------------- number cards

def _chunk_rates(days, part_key, whole_key, size=5):
    """A rate for every `size` days, carrying the last known rate over quiet stretches."""
    out, last = [], 0
    for i in range(0, len(days), size):
        chunk = days[i:i + size]
        whole = sum(d[whole_key] for d in chunk)
        if whole:
            last = sum(d[part_key] for d in chunk) / whole
        out.append(last)
    return out


def number_cards(org, now=None):
    """The four cards for the last 30 days."""
    from delivery.models import Message
    from people.models import Person

    now = now or timezone.now()
    today = now.astimezone(org.tz).date()
    dates = [today - timedelta(days=29 - i) for i in range(30)]
    since = _midnight(org, dates[0])
    before = since - timedelta(days=30)
    delivered = _delivered()

    # Subscribers: people with at least one newsletter subscription switched on.
    subscribed = Person.objects.filter(subscriptions__list__kind="campaign", subscriptions__state="on").distinct()
    subscribers = subscribed.count()
    firsts = _first_subscriptions(since)
    gained_by_day = {}
    for _person, first in firsts:
        day = first.astimezone(org.tz).date()
        gained_by_day[day] = gained_by_day.get(day, 0) + 1
    lost = (
        Person.objects.filter(subscriptions__list__kind="campaign", subscriptions__state="off", subscriptions__updated_at__gte=since)
        .exclude(pk__in=subscribed.values("pk")).distinct().count()
    )
    net = len(firsts) - lost
    running, trend = subscribers, []
    for day in reversed(dates):
        trend.append(running)
        running -= gained_by_day.get(day, 0)
    trend.reverse()

    # Messages sent, every channel and source.
    sent_rows = (
        Message.objects.filter(delivered, sent_at__gte=since)
        .annotate(d=TruncDate("sent_at", tzinfo=org.tz)).values("d", "channel").annotate(n=Count("id"))
    )
    sent_by_day, channels = {}, set()
    for row in sent_rows:
        sent_by_day[row["d"]] = sent_by_day.get(row["d"], 0) + row["n"]
        channels.add(row["channel"])
    sent = sum(sent_by_day.values())

    # Open and click rate: newsletter email only.
    news = Message.objects.filter(delivered, message_type="newsletter", channel="email")
    counts = {"n": Count("id"), "opened": Count("id", filter=OPENED), "clicked": Count("id", filter=CLICKED)}
    current = news.filter(sent_at__gte=since).aggregate(**counts)
    previous = news.filter(sent_at__gte=before, sent_at__lt=since).aggregate(**counts)
    by_day = {
        row["d"]: row for row in
        news.filter(sent_at__gte=since).annotate(d=TruncDate("sent_at", tzinfo=org.tz)).values("d").annotate(**counts)
    }
    days = [by_day.get(day, {"n": 0, "opened": 0, "clicked": 0}) for day in dates]

    def rate(row, key):
        return row[key] / row["n"] if row["n"] else None

    def points(key):
        a, b = rate(current, key), rate(previous, key)
        return None if a is None or b is None else round((a - b) * 100, 1)

    return {
        "subscribers": {"value": subscribers, "net": net, "spark": charts.sparkline(trend)},
        "sent": {
            "value": sent,
            "channels": _channel_words(channels),
            "spark": charts.sparkline([sent_by_day.get(day, 0) for day in dates], color=charts.PEACH),
        },
        "open_rate": {"value": rate(current, "opened"), "points": points("opened"), "spark": charts.sparkline(_chunk_rates(days, "opened", "n"))},
        "click_rate": {"value": rate(current, "clicked"), "points": points("clicked"),
                       "spark": charts.sparkline(_chunk_rates(days, "clicked", "n"), color=charts.PEACH)},
    }


# --------------------------------------------------------------------------- engagement

def _day_label(day):
    return dateformat.format(day, "M j")


def engagement(org, days=30, now=None):
    """Opens, link clicks and new subscribers on one time axis.

    Daily buckets for 7 and 30 days; for 90 days, thirteen whole weeks.
    """
    from campaigns.models import Campaign
    from delivery.models import LinkClick, Message

    if days not in (7, 30, 90):
        days = 30
    now = now or timezone.now()
    today = now.astimezone(org.tz).date()
    weekly = days == 90
    size = 7 if weekly else 1
    count = 13 if weekly else days
    first_day = today - timedelta(days=count * size - 1)
    since = _midnight(org, first_day)

    def bucket(day):
        return min(count - 1, max(0, (day - first_day).days // size))

    opens = [0] * count
    by_campaign = [dict() for _i in range(count)]
    rows = (
        Message.objects.filter(opened_at__gte=since)
        .annotate(d=TruncDate("opened_at", tzinfo=org.tz)).values("d", "campaign_id").annotate(n=Count("id"))
    )
    for row in rows:
        if row["d"] is None:
            continue
        i = bucket(row["d"])
        opens[i] += row["n"]
        if row["campaign_id"]:
            by_campaign[i][row["campaign_id"]] = by_campaign[i].get(row["campaign_id"], 0) + row["n"]

    clicks = [0] * count
    for row in LinkClick.objects.filter(at__gte=since).annotate(d=TruncDate("at", tzinfo=org.tz)).values("d").annotate(n=Count("id")):
        if row["d"] is not None:
            clicks[bucket(row["d"])] += row["n"]

    subscribers = [0] * count
    for _person, first in _first_subscriptions(since):
        subscribers[bucket(first.astimezone(org.tz).date())] += 1

    starts = [first_day + timedelta(days=i * size) for i in range(count)]
    if count <= 7:
        marks = list(range(count))
    else:
        marks = sorted({round(i * (count - 1) / 4) for i in range(5)})
    x_labels = [_day_label(starts[i]) for i in marks]

    open_max = charts.nice_max(opens, floor=3)
    click_max = charts.nice_max(clicks, floor=2)
    sub_max = charts.nice_max(subscribers, floor=4)
    peak = charts.peak(opens, height=190, vmax=open_max)
    if peak:
        top = max(by_campaign[peak["index"]].items(), key=lambda kv: kv[1], default=None)
        name = Campaign.objects.filter(pk=top[0]).values_list("name", flat=True).first() if top else None
        if not name:
            name = _("Week of %(day)s") % {"day": _day_label(starts[peak["index"]])} if weekly else _day_label(starts[peak["index"]])
        peak["name"] = name

    # What the interactive charts (static/js/charts.js) draw: every bucket's label, and for
    # opens the campaign that caused most of each bucket.
    def bucket_label(i):
        return _("Week of %(day)s") % {"day": _day_label(starts[i])} if weekly else _day_label(starts[i])

    top_ids = [max(b.items(), key=lambda kv: kv[1])[0] if b else None for b in by_campaign]
    names = dict(Campaign.objects.filter(pk__in=[i for i in top_ids if i]).values_list("pk", "name"))
    labels = [bucket_label(i) for i in range(count)]
    chart_data = {
        "group": "engagement",
        "charts": [
            {"canvas": "ch-opens", "type": "line", "labels": labels, "values": opens, "color": charts.TEAL,
             "unit": [_("open"), _("opens")], "notes": [names.get(i, "") for i in top_ids],
             "peak": peak["index"] if peak else None, "suggestedMax": 3, "yTicks": 4},
            {"canvas": "ch-clicks", "type": "bar", "labels": labels, "values": clicks, "color": charts.PEACH,
             "unit": [_("link click"), _("link clicks")], "suggestedMax": 2, "yTicks": 3},
            {"canvas": "ch-subscribers", "type": "bar", "labels": labels, "values": subscribers, "color": charts.TEAL_SOFT,
             "unit": [_("new subscriber"), _("new subscribers")], "suggestedMax": 4, "yTicks": 3},
        ],
    }

    span = ngettext("the last %(n)s day", "the last %(n)s days", days) % {"n": days}
    open_label = _("%(n)s opens over %(span)s") % {"n": f"{sum(opens):,}", "span": span}
    if peak:
        open_label += ", " + _("peaking at %(n)s for %(name)s") % {"n": f"{peak['value']:,}", "name": peak["name"]}
    click_label = _("%(n)s link clicks over %(span)s") % {"n": f"{sum(clicks):,}", "span": span}
    sub_label = _("%(n)s new subscribers over %(span)s") % {"n": f"{sum(subscribers):,}", "span": span}
    return {
        "days": days,
        "ranges": [{"days": d, "label": _(label), "current": d == days} for d, label in RANGES],
        "weekly": weekly,
        "empty": not (sum(opens) or sum(clicks) or sum(subscribers)),
        "opens": {
            "values": opens, "total": sum(opens), "axis": charts.axis_labels(open_max, 3), "peak": peak, "label": open_label,
            "svg": charts.area_chart(opens, height=190, vmax=open_max, parts=3, label=open_label),
        },
        "clicks": {
            "values": clicks, "total": sum(clicks), "axis": charts.axis_labels(click_max, 2), "label": click_label,
            "svg": charts.bar_chart(clicks, height=110, vmax=click_max, parts=2, color=charts.PEACH, dim=0.78,
                                    highlight=peak["index"] if peak else None,
                                    label=_("%(n)s link clicks over %(span)s") % {"n": f"{sum(clicks):,}", "span": span}),
        },
        "subscribers": {
            "values": subscribers, "total": sum(subscribers), "axis": charts.axis_labels(sub_max, 1), "label": sub_label,
            "svg": charts.bar_chart(subscribers, height=90, vmax=sub_max, parts=1, color=charts.TEAL_SOFT,
                                    label=_("%(n)s new subscribers over %(span)s") % {"n": f"{sum(subscribers):,}", "span": span}),
        },
        "x_labels": x_labels,
        "chart": chart_data,
    }


# --------------------------------------------------------------------------- from the village today

def village_today(org, now=None):
    """What each building sent today, the state of the outbox and how much arrived."""
    from delivery.models import Message

    now = now or timezone.now()
    since = _midnight(org, now.astimezone(org.tz).date())
    today = Message.objects.filter(created_at__gte=since).exclude(state="skipped")
    grouped = {}
    rows = (
        today.filter(source="building").values("building_id", "template_id", "template__name", "channel").annotate(n=Count("id"))
    )
    for row in rows:
        entry = grouped.setdefault((row["building_id"], row["template_id"]), {
            "building_id": row["building_id"], "name": row["template__name"] or _("Messages"), "count": 0, "channels": set()})
        entry["count"] += row["n"]
        entry["channels"].add(row["channel"])
    buildings = {b.pk: b for b in Building.objects.filter(pk__in={e["building_id"] for e in grouped.values() if e["building_id"]})}
    senders = []
    for entry in sorted(grouped.values(), key=lambda e: -e["count"])[:6]:
        building = buildings.get(entry["building_id"])
        senders.append({
            "building": building,
            "name": entry["name"],
            "detail": " · ".join(x for x in [building.name if building else "", _channel_words(entry["channels"])] if x),
            "count": entry["count"],
        })
    totals = today.aggregate(
        total=Count("id"), delivered=Count("id", filter=_delivered()), failed=Count("id", filter=Q(state="failed")))
    waiting = Message.objects.filter(state__in=("queued", "sending")).count()
    last_sent = Message.objects.aggregate(last=Max("sent_at"))["last"]
    return {
        "senders": senders,
        "waiting": waiting,
        "last_sent": last_sent,
        "total": totals["total"],
        "delivered": totals["delivered"],
        "failed": totals["failed"],
        "delivered_width": round(totals["delivered"] / totals["total"] * 100, 1) if totals["total"] else 0,
        "failed_url": "/outbox/?status=failed",
    }


# --------------------------------------------------------------------------- audience

def audience(org):
    from people.models import Person

    total = Person.objects.count()
    by_code = dict(Person.objects.values_list("language__code").annotate(n=Count("id")))
    languages = []
    for i, lang in enumerate(Language.objects.all()):
        n = by_code.get(lang.code, 0)
        languages.append({
            "language": lang, "count": n, "share": (n / total) if total else 0,
            "width": round(n / total * 100, 1) if total else 0, "colour": LANGUAGE_COLOURS[i % len(LANGUAGE_COLOURS)],
        })
    reach = [
        ("email", _("Email"), Person.objects.filter(email__isnull=False, email_state="ok").exclude(email="").count()),
        ("telegram", _("Telegram"), Person.objects.filter(telegram_links__blocked_at__isnull=True, telegram_links__isnull=False).distinct().count()),
        ("push", _("Push"), Person.objects.filter(push_memberships__isnull=False).distinct().count()),
    ]
    return {
        "total": total,
        "languages": languages,
        "channels": [{"key": key, "name": name, "count": n, "width": round(n / total * 100, 1) if total else 0} for key, name, n in reach],
    }


# --------------------------------------------------------------------------- the view

@role_required("viewer")
def dashboard(request):
    from campaigns.models import Campaign
    from campaigns.reports import campaign_rows

    org = Organization.get()
    try:
        days = int(request.GET.get("range", 30))
    except ValueError:
        days = 30
    chart = engagement(org, days)
    if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Target") == "po-engagement":
        return render(request, "core/dashboard_engagement.html", {"engagement": chart})
    return render(request, "core/dashboard.html", {
        "greeting": greeting(org, request.user),
        "status": status_line(),
        "cards": number_cards(org),
        "engagement": chart,
        "village": village_today(org),
        "recent": campaign_rows(Campaign.objects.all()[:5]),
        "audience": audience(org),
    })
