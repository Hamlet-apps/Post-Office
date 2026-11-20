"""Numbers for the campaign report, the campaigns list and the dashboard.

    message_stats(campaign)      counts read from the outbox rows
    stats_from_totals(totals)    the same counts read from Campaign.totals once rows are gone
    report_extras(campaign)      what the retention job can add to Campaign.totals
    campaign_report(campaign)    everything the report screen shows
    campaign_rows(campaigns)     one summary per campaign for lists of campaigns

Telegram has no open signal: for Telegram the report gives delivered and
button taps only. Open rates are always email opens over email delivered.
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import dateformat, timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from content.models import Content
from core import charts
from core.models import Language, Organization
from delivery.models import Message, TrackedLink
from people.models import Person, Subscription

from .checks import collect_links

DELIVERED = Q(state__in=Message.DELIVERED_STATES)
# A click on an email link implies it was opened; a Telegram tap says nothing about opening.
OPENED = Q(opened_at__isnull=False) | Q(state="opened") | Q(state="clicked", channel="email")
CLICKED = Q(clicked_at__isnull=False) | Q(state="clicked")
COUNT_KEYS = ("sent", "delivered", "opened", "clicked", "failed", "skipped")
CHANNEL_NAMES = {"email": "Email", "telegram": "Telegram", "push": "Push"}


def rate(part, whole):
    """A share between 0 and 1, or None when there is nothing to divide by."""
    return (part / whole) if whole else None


def _blank():
    return {key: 0 for key in COUNT_KEYS}


def _annotations():
    return {
        "total": Count("id"),
        "n_skipped": Count("id", filter=Q(state="skipped")),
        "n_failed": Count("id", filter=Q(state="failed")),
        "n_delivered": Count("id", filter=DELIVERED),
        "n_opened": Count("id", filter=DELIVERED & OPENED),
        "n_clicked": Count("id", filter=DELIVERED & CLICKED),
    }


def _counts(row):
    return {
        "sent": row["total"] - row["n_skipped"],
        "delivered": row["n_delivered"],
        "opened": row["n_opened"],
        "clicked": row["n_clicked"],
        "failed": row["n_failed"],
        "skipped": row["n_skipped"],
    }


def _add(into, counts):
    for key in COUNT_KEYS:
        into[key] = into.get(key, 0) + counts.get(key, 0)


def _finish(stats):
    """Add the two bases that rates are measured against."""
    by_channel = stats.get("by_channel") or {}
    email = by_channel.get("email")
    stats["email_delivered"] = email["delivered"] if email else stats.get("email_delivered", 0 if by_channel else stats.get("delivered", 0))
    stats["email_opened"] = email["opened"] if email else stats.get("email_opened", 0 if by_channel else stats.get("opened", 0))
    if by_channel:
        stats["click_base"] = sum(by_channel.get(ch, {}).get("delivered", 0) for ch in ("email", "telegram"))
    else:
        stats.setdefault("click_base", stats.get("delivered", 0))
    stats["open_rate"] = rate(stats["email_opened"], stats["email_delivered"])
    stats["click_rate"] = rate(stats.get("clicked", 0), stats["click_base"])
    stats["delivered_rate"] = rate(stats.get("delivered", 0), stats.get("sent", 0))
    return stats


def message_stats(campaign):
    """Counts from the outbox rows, overall, by channel and by language. None when there are no rows."""
    rows = list(Message.objects.filter(campaign=campaign).values("channel", "language").annotate(**_annotations()))
    if not rows:
        return None
    stats = _blank()
    stats.update({"by_channel": {}, "by_language": {}})
    for row in rows:
        counts = _counts(row)
        _add(stats, counts)
        _add(stats["by_channel"].setdefault(row["channel"], _blank()), counts)
        lang = stats["by_language"].setdefault(row["language"] or Language.default_code(), dict(_blank(), email_delivered=0, email_opened=0))
        _add(lang, counts)
        if row["channel"] == "email":
            lang["email_delivered"] += counts["delivered"]
            lang["email_opened"] += counts["opened"]
    return _finish(stats)


def _from_bucket(bucket):
    """One bucket of delivery.pipeline.campaign_totals in this module's words.

    There "messages" is everything that was attempted and "sent" is what arrived.
    """
    def number(key):
        try:
            return int(bucket.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    if "messages" in bucket:
        attempted, delivered = number("messages"), number("sent")
    else:  # a plain {"sent", "delivered", ...} summary
        delivered = number("delivered") or number("sent")
        attempted = max(number("sent"), delivered + number("failed"))
    return {"sent": attempted, "delivered": delivered, "opened": number("opened"), "clicked": number("clicked"),
            "failed": number("failed"), "skipped": number("skipped")}


def stats_from_totals(totals):
    """Read Campaign.totals (kept after the 180-day retention) into the same shape as message_stats."""
    totals = totals if isinstance(totals, dict) else {}
    if not totals:
        return None
    stats = _from_bucket(totals)
    stats["by_channel"] = {k: _from_bucket(v) for k, v in (totals.get("by_channel") or {}).items() if isinstance(v, dict)}
    stats["by_language"] = {}
    for code, bucket in (totals.get("by_language") or {}).items():
        if isinstance(bucket, dict):
            counts = _from_bucket(bucket)
            # Totals do not split a language by channel, so its open rate is over everything delivered.
            stats["by_language"][code] = dict(counts, email_delivered=counts["delivered"], email_opened=counts["opened"])
    return _finish(stats)


def campaign_stats(campaign):
    """(stats, from_rows). Falls back to Campaign.totals when the message rows are gone."""
    stats = message_stats(campaign)
    if stats is not None:
        return stats, True
    return stats_from_totals(campaign.totals), False


# --------------------------------------------------------------------------- report parts

def started_at(campaign):
    if campaign.started_at:
        return campaign.started_at
    first = Message.objects.filter(campaign=campaign, sent_at__isnull=False).order_by("sent_at").values_list("sent_at", flat=True).first()
    return first or campaign.sent_at or campaign.scheduled_at


def unsubscribed_count(campaign, start=None):
    """People who received the campaign and switched one of its lists off afterwards."""
    start = start or started_at(campaign)
    if start is None:
        return 0
    recipients = Message.objects.filter(campaign=campaign).filter(DELIVERED).values("person_id")
    return (
        Subscription.objects.filter(list__in=campaign.lists.all(), state="off", updated_at__gte=start, person_id__in=recipients)
        .values("person_id").distinct().count()
    )


def opens_by_hour(campaign, start=None, hours=48):
    """Opens in each of the first `hours` hours after the send started."""
    start = start or started_at(campaign)
    buckets = [0] * hours
    if start is None:
        return buckets
    end = start + timedelta(hours=hours)
    times = Message.objects.filter(campaign=campaign, opened_at__gte=start, opened_at__lt=end).values_list("opened_at", flat=True)
    for at in times:
        buckets[min(hours - 1, int((at - start).total_seconds() // 3600))] += 1
    return buckets


def failure_reasons(campaign):
    """Messages that did not arrive, grouped by reason, most common first."""
    rows = (
        Message.objects.filter(campaign=campaign, state__in=("failed", "skipped"))
        .values("state", "error").annotate(n=Count("id")).order_by("-n", "error")
    )
    out = []
    for row in rows:
        reason = (row["error"] or "").strip() or (_("Skipped, no reason recorded") if row["state"] == "skipped" else _("Failed, no reason recorded"))
        out.append({"reason": reason, "count": row["n"], "state": row["state"]})
    return out


def link_rows(campaign):
    """Tracked links with their clicks and share of all clicks."""
    labels = dict(collect_links(campaign))
    links = list(TrackedLink.objects.filter(campaign=campaign).annotate(n=Count("clicks")).order_by("-n", "id"))
    total = sum(link.n for link in links)
    return [
        {"url": link.url, "short": _short_url(link.url), "label": labels.get(link.url, ""), "clicks": link.n, "share": rate(link.n, total)}
        for link in links
    ]


def _short_url(url):
    short = url.split("://", 1)[-1]
    if short.startswith("www."):
        short = short[4:]
    return short.rstrip("/")


def _hour_label(dt, with_day=False):
    text = dateformat.format(dt, "g A") if dt.minute == 0 else dateformat.format(dt, "g:i A")
    return f"{dateformat.format(dt, 'M j')}, {text}" if with_day else text


def _opens_chart(values, start, tz):
    total = sum(values)
    vmax = charts.nice_max(values, floor=3)
    labels = []
    if start is not None:
        local = start.astimezone(tz)
        for step in range(5):
            at = local + timedelta(hours=12 * step)
            labels.append(_hour_label(at, with_day=step in (2, 4)))
    half_hours = None
    if total:
        running = 0
        for hour, n in enumerate(values, start=1):
            running += n
            if running * 2 >= total:
                half_hours = hour
                break
    if not total:
        summary = _("No opens recorded in the first 48 hours.")
    elif half_hours == 1:
        summary = _("Half of all opens came within an hour of sending.")
    else:
        summary = ngettext(
            "Half of all opens came within %(n)s hour of sending.",
            "Half of all opens came within %(n)s hours of sending.", half_hours,
        ) % {"n": half_hours}
    if start is not None:
        local = start.astimezone(tz)
        hour_labels = [_hour_label(local + timedelta(hours=h), with_day=True) for h in range(len(values))]
    else:
        hour_labels = [_("Hour %(n)s") % {"n": h + 1} for h in range(len(values))]
    chart_data = {"charts": [{
        "canvas": "ch-report-opens", "type": "line", "labels": hour_labels, "values": values, "color": charts.TEAL,
        "unit": [_("open"), _("opens")], "suggestedMax": 3, "yTicks": 4, "ticks": 4,
    }]}
    return {
        "chart": chart_data,
        "values": values,
        "total": total,
        "svg": charts.area_chart(values, height=200, vmax=vmax, parts=3, label=_("%(n)s opens in the first 48 hours") % {"n": f"{total:,}"}),
        "axis": charts.axis_labels(vmax, 3),
        "labels": labels,
        "summary": summary,
    }


def _channel_rows(stats):
    rows = []
    for channel in ("email", "telegram", "push"):
        counts = (stats.get("by_channel") or {}).get(channel)
        if not counts:
            continue
        delivered = counts["delivered"]
        if channel == "email":
            opened = rate(counts["opened"], delivered)
            detail = _("%(n)s delivered") % {"n": f"{delivered:,}"}
            if opened is not None:
                detail += " · " + _("%(pct)s opened") % {"pct": f"{opened * 100:.1f}%"}
            number = counts["opened"]
            number_label = _("opened")
        elif channel == "telegram":
            detail = _("%(n)s delivered") % {"n": f"{delivered:,}"} + " · " + ngettext(
                "%(n)s tapped the button", "%(n)s tapped the button", counts["clicked"]) % {"n": f"{counts['clicked']:,}"}
            number = delivered
            number_label = _("delivered")
        else:
            detail = _("%(n)s delivered") % {"n": f"{delivered:,}"}
            number = delivered
            number_label = _("delivered")
        rows.append({"channel": channel, "name": _(CHANNEL_NAMES[channel]), "icon": {"email": "mail", "telegram": "telegram", "push": "push"}[channel],
                     "detail": detail, "number": number, "number_label": number_label, "counts": counts})
    return rows


def _language_rows(stats):
    by_code = stats.get("by_language") or {}
    names = {lang.code: lang for lang in Language.objects.all()}
    rows = []
    for code in list(names) + [c for c in by_code if c not in names]:
        counts = by_code.get(code)
        if not counts:
            continue
        lang = names.get(code)
        open_rate = rate(counts.get("email_opened", counts["opened"]), counts.get("email_delivered", counts["delivered"]))
        click_rate = rate(counts["clicked"], counts["delivered"])
        rows.append({
            "code": code, "name": lang.name if lang else code.upper(), "sent": counts["sent"],
            "opened": counts["opened"], "clicked": counts["clicked"],
            "open_rate": open_rate, "click_rate": click_rate,
            "open_width": round((open_rate or 0) * 100, 1), "click_width": round((click_rate or 0) * 100, 1),
        })
    return rows


def campaign_report(campaign):
    """Everything the report screen shows."""
    org = Organization.get()
    stats, from_rows = campaign_stats(campaign)
    totals = campaign.totals if isinstance(campaign.totals, dict) else {}
    start = started_at(campaign)
    if stats is None:
        stats = _finish(dict(_blank(), by_channel={}, by_language={}))
    if from_rows:
        unsubscribed = unsubscribed_count(campaign, start)
        failures = failure_reasons(campaign)
        opens = opens_by_hour(campaign, start)
        links = link_rows(campaign)
    else:
        unsubscribed = int(totals.get("unsubscribed") or 0)
        failures = [f for f in (totals.get("failures") or []) if isinstance(f, dict) and "reason" in f]
        opens = list(totals.get("opens_48h") or []) or [0] * 48
        links = [l for l in (totals.get("links") or []) if isinstance(l, dict) and "url" in l]
        clicks = sum(l.get("clicks", 0) for l in links)
        for l in links:
            l.setdefault("short", _short_url(l["url"]))
            l.setdefault("label", "")
            l["share"] = rate(l.get("clicks", 0), clicks)
    not_arrived = sum(f["count"] for f in failures) if failures else stats["failed"] + stats["skipped"]
    return {
        "stats": stats,
        "from_rows": from_rows,
        "has_data": bool(stats["sent"] or stats["skipped"]),
        "started_at": start,
        "unsubscribed": unsubscribed,
        "unsubscribed_rate": rate(unsubscribed, stats["delivered"]),
        "opens": _opens_chart(opens, start, org.tz),
        "languages": _language_rows(stats),
        "channels": _channel_rows(stats),
        "links": links,
        "failures": failures,
        "not_arrived": not_arrived,
        "outbox_url": f"/outbox/?campaign={campaign.public_id}&status=failed",
    }


def report_extras(campaign):
    """The parts of the report that cannot be rebuilt once message rows are deleted.

    The retention job can merge this into Campaign.totals before deleting rows:
        totals.update(campaigns.reports.report_extras(campaign))
    """
    start = started_at(campaign)
    return {
        "unsubscribed": unsubscribed_count(campaign, start),
        "failures": [{"reason": f["reason"], "count": f["count"]} for f in failure_reasons(campaign)],
        "links": [{"url": l["url"], "label": l["label"], "clicks": l["clicks"]} for l in link_rows(campaign)],
        "opens_48h": opens_by_hour(campaign, start),
    }


# --------------------------------------------------------------------------- lists of campaigns

STATE_TAGS = {
    "draft": "",
    "scheduled": "po-tag--peach",
    "sending": "po-tag--ok",
    "sent": "po-tag--teal",
    "paused": "po-tag--peach",
    "cancelled": "po-tag--danger",
}


def campaign_url(campaign):
    """Where a campaign's name leads: design for a draft, send for a scheduled one, else the report."""
    if campaign.state == "draft":
        return f"/campaigns/{campaign.public_id}/design/"
    if campaign.state == "scheduled":
        return f"/campaigns/{campaign.public_id}/send/"
    return f"/campaigns/{campaign.public_id}/"


def _when(campaign, tz, now):
    if campaign.state == "scheduled" and campaign.scheduled_at:
        return dateformat.format(campaign.scheduled_at.astimezone(tz), "M j, g:i A")
    if campaign.state == "draft":
        days = (now - campaign.updated_at).days
        if days <= 0:
            return _("Edited today")
        return ngettext("Edited %(n)s day ago", "Edited %(n)s days ago", days) % {"n": days}
    at = campaign.started_at or campaign.sent_at or campaign.scheduled_at or campaign.updated_at
    local = at.astimezone(tz)
    return dateformat.format(local, "M j" if local.year == now.astimezone(tz).year else "M j, Y")


def campaign_rows(campaigns):
    """Summaries for a page of campaigns, with a fixed number of queries whatever the page size."""
    campaigns = list(campaigns)
    if not campaigns:
        return []
    org = Organization.get()
    tz, now = org.tz, timezone.now()
    ids = [c.pk for c in campaigns]
    languages = list(Language.objects.all())
    default_code = Language.default_code()

    written = {}
    for content in Content.objects.filter(campaign_id__in=ids):
        if content.is_written:
            written.setdefault((content.campaign_id, content.form), set()).add(content.language)

    per_campaign = {}
    for row in Message.objects.filter(campaign_id__in=ids).values("campaign_id", "channel").annotate(**_annotations()):
        stats = per_campaign.setdefault(row["campaign_id"], dict(_blank(), by_channel={}))
        counts = _counts(row)
        _add(stats, counts)
        _add(stats["by_channel"].setdefault(row["channel"], _blank()), counts)

    lists_of = {}
    through = campaigns[0].lists.through
    list_ids = set()
    for campaign_id, list_id in through.objects.filter(campaign_id__in=ids).values_list("campaign_id", "list_id"):
        lists_of.setdefault(campaign_id, []).append(list_id)
        list_ids.add(list_id)
    from people.models import List

    list_names = {l.pk: l.display_name(default_code) for l in List.objects.filter(pk__in=list_ids)}

    rows = []
    for campaign in campaigns:
        channels = [c for c in ("email", "telegram", "push") if c in (campaign.channels or [])]
        forms = (["email"] if "email" in channels else []) + (["notification"] if {"telegram", "push"} & set(channels) else [])
        forms = forms or ["email"]
        tags, missing = [], []
        for lang in languages:
            ok = all(lang.code in written.get((campaign.pk, form), ()) for form in forms)
            anywhere = any(lang.code in written.get((campaign.pk, form), ()) for form in ("email", "notification"))
            if campaign.state in ("sent", "sending", "paused", "cancelled") and not anywhere:
                continue  # a sent campaign only lists the languages it went out in
            tags.append({"code": lang.code, "label": lang.code.upper(), "written": ok, "name": lang.english_name or lang.name})
            if not ok:
                missing.append(lang.english_name or lang.name)
        if not missing:
            missing_text = ""
        elif len(missing) == 1:
            missing_text = _("%(language)s not written yet") % {"language": missing[0]}
        else:
            missing_text = _("%(n)s languages to go") % {"n": len(missing)}

        stats = per_campaign.get(campaign.pk)
        stats = _finish(stats) if stats else stats_from_totals(campaign.totals)
        names = [list_names[i] for i in lists_of.get(campaign.pk, []) if i in list_names]
        if stats:
            audience = stats["sent"]
        elif campaign.state in ("draft", "scheduled") and lists_of.get(campaign.pk) and channels:
            audience = (
                Person.objects.filter(subscriptions__list_id__in=lists_of[campaign.pk], subscriptions__channel__in=channels,
                                      subscriptions__state="on").distinct().count()
            )
        else:
            audience = None
        rows.append({
            "campaign": campaign,
            "url": campaign_url(campaign),
            "state_tag": STATE_TAGS.get(campaign.state, ""),
            "channels": [{"key": c, "name": _(CHANNEL_NAMES[c]), "icon": {"email": "mail", "telegram": "telegram", "push": "push"}[c]} for c in channels],
            "languages": tags,
            "missing": missing_text,
            "lists": names,
            "to": ", ".join(names),
            "audience": audience,
            "open_rate": stats["open_rate"] if stats and campaign.state not in ("draft", "scheduled") else None,
            "click_rate": stats["click_rate"] if stats and campaign.state not in ("draft", "scheduled") else None,
            "when": _when(campaign, tz, now),
            # Named beside the date only when the campaign does not go by email.
            "channel_note": "" if "email" in channels else ", ".join(str(_(CHANNEL_NAMES[c])) for c in channels),
        })
    return rows

