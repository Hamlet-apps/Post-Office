"""The sending pipeline (spec section 7).

Every send, from a campaign, a building or a quick message, goes through the
same steps: resolve recipients, fan out by channel, pick the language, write
Message rows, queue, send, record. The rule that shapes it: a person gets the
message on every channel they have switched on, and no message is sent twice.

Other areas call:

    send_campaign(campaign) -> int
    send_app_message(template, person, data=None, building=None, request=None) -> list[Message]
    send_quick_message(quick_message) -> int
    send_system_email(to_email, subject, html, text="", person=None) -> Message | None
    send_test_email(sender, to_email, rendered) -> (ok, error)
    audience(lists, channels, sender=None, bot=None, push_apps=()) -> dict
    retry_message(message), pause_campaign(c), resume_campaign(c), cancel_campaign(c)
    links_for(person, message=None) -> dict
"""
import datetime
import logging
import uuid
from collections import defaultdict
from functools import partial

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from campaigns.models import Campaign
from channels import email as email_channel
from channels import push as push_channel
from channels import telegram as telegram_channel
from channels.models import EmailSender, PushMember, TelegramBot, TelegramLink
from content import render
from content.models import Content, owner_filter
from core.models import Language, Organization
from people.models import CHANNELS, List, Person, Subscription
from people.services import reachable_channels, set_subscription
from people.tokens import preferences_url

from . import limits
from .models import Message, TrackedLink

log = logging.getLogger(__name__)

RETRY_MINUTES = (1, 5, 30, 120)
INSERT_CHUNK = 1000
SEND_CHUNK = 100
STUCK_AFTER_MINUTES = 10
RETENTION_DAYS = 180
HELD_CAMPAIGN_STATES = ("paused", "cancelled")
INTERRUPTED = "interrupted, not retried"


# --------------------------------------------------------------------------- links

def links_for(person, message=None):
    """The links a rendered message may use; pass to content.render.build_context."""
    links = {"preferences": preferences_url(person) if person is not None and person.pk else ""}
    if message is not None and message.message_type == "newsletter":
        links["unsubscribe"] = f"{settings.PUBLIC_URL}/u/{message.token}"
    return links


def _footer_telegram_link(person, org):
    """Optional "Get this on Telegram" footer link (signup form option telegram_in_footer)."""
    if not org.signup_option("telegram_in_footer", False):
        return ""
    bot = TelegramBot.default()
    if bot is None or not bot.username or person.telegram_links.filter(bot=bot).exists():
        return ""
    from people.tokens import connect_url

    return connect_url(person, bot)


# --------------------------------------------------------------------------- step 1 and 2: who, on which channels

def _reach(lists, channels, sender, bot, push_apps):
    """Resolve recipients and fan out by channel, without writing anything.

    Yields (person_id, language_code, targets, reasons) for every person with at
    least one subscription that is on for one of the lists. targets is a list of
    (channel, list_id, push_app); reasons says why a channel was left out.
    """
    list_ids = [lst.pk for lst in lists]
    if not list_ids:
        return
    on = defaultdict(dict)  # person id -> {channel: list id}
    for pid, channel, list_id in (Subscription.objects.filter(list_id__in=list_ids, state="on")
                                  .order_by("list__position", "list_id")
                                  .values_list("person_id", "channel", "list_id").iterator(chunk_size=5000)):
        on[pid].setdefault(channel, list_id)
    connected = set()
    if bot is not None and "telegram" in channels:
        connected = set(TelegramLink.objects.filter(bot=bot, blocked_at__isnull=True).values_list("person_id", flat=True))
    members = {}
    if "push" in channels:
        for app in push_apps:
            members[app.pk] = set(PushMember.objects.filter(push_app=app).values_list("person_id", flat=True))
    default_code = Language.default_code()
    people = (Person.objects.filter(subscriptions__list_id__in=list_ids, subscriptions__state="on").distinct()
              .order_by("pk").values_list("pk", "email", "email_state", "language__code"))
    for pid, address, email_state, code in people.iterator(chunk_size=5000):
        targets, reasons = [], []
        mine = on.get(pid, {})
        for channel in channels:
            if channel not in mine:
                reasons.append({
                    "email": _("has not signed up for this by email"),
                    "telegram": _("has not signed up for this on Telegram"),
                    "push": _("has not signed up for this as a notification"),
                }[channel])
                continue
            if channel == "email":
                if sender is None:
                    reasons.append(_("no email sender is set up"))
                elif not address:
                    reasons.append(_("has no email address"))
                elif email_state != "ok":
                    reasons.append(_("email to this address bounced earlier"))
                else:
                    targets.append(("email", mine[channel], None))
            elif channel == "telegram":
                if bot is None:
                    reasons.append(_("no Telegram bot is set up"))
                elif pid not in connected:
                    reasons.append(_("is not connected to the Telegram bot"))
                else:
                    targets.append(("telegram", mine[channel], None))
            elif channel == "push":
                found = False
                for app in push_apps:
                    if pid in members.get(app.pk, ()):
                        targets.append(("push", mine[channel], app))
                        found = True
                if not found:
                    reasons.append(_("has not allowed notifications in the app"))
        yield pid, code or default_code, targets, reasons


def audience(lists, channels, sender=None, bot=None, push_apps=()):
    """Who a campaign would reach right now. Creates nothing.

    Returns {"people", "email", "telegram", "push", "by_language", "without_consent"}.
    """
    lists = list(lists)
    channels = [c for c in CHANNELS if c in (channels or [])]
    push_apps = list(push_apps or [])
    if "email" in channels and sender is None:
        sender = EmailSender.default_newsletter()
    if "telegram" in channels and bot is None:
        bot = TelegramBot.default()
    out = {"people": 0, "email": 0, "telegram": 0, "push": 0, "by_language": {}, "without_consent": 0}
    for _pid, code, targets, _reasons in _reach(lists, channels, sender, bot, push_apps):
        if not targets:
            continue
        out["people"] += 1
        out["by_language"][code] = out["by_language"].get(code, 0) + 1
        for channel in {t[0] for t in targets}:
            out[channel] += 1
    out["without_consent"] = (Subscription.objects.filter(
        list__in=[lst for lst in lists if lst.kind == "campaign"], state="on", channel__in=channels,
        consent__isnull=True).values("person_id").distinct().count())
    return out


# --------------------------------------------------------------------------- step 3: language

def _written(owner, form):
    return {c.language: c for c in Content.objects.filter(form=form, **owner_filter(owner)) if c.is_written}


def _pick_language(written, code, default_code, service=False):
    """Return (language, used_fallback); language is None when nothing usable is written."""
    if code in written:
        return code, False
    if default_code in written:
        return default_code, True
    if service and written:
        # Service messages are never dropped for a missing translation.
        return next(iter(written)), True
    return None, False


def _form_for(channel):
    return "email" if channel == "email" else "notification"


# --------------------------------------------------------------------------- step 4 and 5: write rows, queue

def _insert(rows):
    for start in range(0, len(rows), INSERT_CHUNK):
        Message.objects.bulk_create(rows[start:start + INSERT_CHUNK], ignore_conflicts=True)


def _dispatch(ids):
    from .tasks import send_batch

    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        send_batch(ids)
        return

    def later():
        try:
            send_batch.delay(ids)
        except Exception:  # noqa: BLE001 - the beat task picks queued messages up anyway
            log.exception("could not queue a batch; send_due_messages will pick it up")

    transaction.on_commit(later)


def enqueue(queryset):
    """One task per chunk of 100 messages per channel (500 per push app). Returns how many were queued."""
    groups = defaultdict(list)
    for pk, channel, app_id in queryset.order_by("pk").values_list("pk", "channel", "push_app_id"):
        groups[(channel, app_id if channel == "push" else None)].append(pk)
    total = 0
    for (channel, _app), ids in groups.items():
        size = limits.PUSH_BATCH if channel == "push" else SEND_CHUNK
        for start in range(0, len(ids), size):
            _dispatch(ids[start:start + size])
            total += len(ids[start:start + size])
    return total


def _due(queryset, now=None):
    queryset = queryset.filter(state="queued").filter(limits.due_filter(now))
    held = list(Campaign.objects.filter(state__in=HELD_CAMPAIGN_STATES).values_list("pk", flat=True))
    return queryset.exclude(campaign_id__in=held) if held else queryset


# --------------------------------------------------------------------------- campaigns

def send_campaign(campaign):
    """Fan a campaign out to its lists and start sending. Safe to call twice.

    Returns the number of messages created (skipped rows are not counted).
    """
    now = timezone.now()
    channels = [c for c in CHANNELS if c in (campaign.channels or [])]
    lists = list(campaign.lists.all())
    sender = campaign.sender or (EmailSender.default_newsletter() if "email" in channels else None)
    bot = campaign.bot or (TelegramBot.default() if "telegram" in channels else None)
    push_apps = list(campaign.push_apps.all())
    first_time = campaign.started_at is None

    campaign.state = "sending"
    if first_time:
        campaign.started_at = now
    campaign.save(update_fields=["state", "started_at", "updated_at"])

    written = {"email": _written(campaign, "email"), "notification": _written(campaign, "notification")}
    default_code = Language.default_code()
    source = campaign.public_id
    before = Message.objects.filter(campaign=campaign).exclude(state="skipped").count()

    reached = list(_reach(lists, channels, sender, bot, push_apps))
    spread = (campaign.spread_minutes or 0) * 60
    rows = []
    for index, (pid, code, targets, reasons) in enumerate(reached):
        send_after = now + datetime.timedelta(seconds=spread * index / len(reached)) if spread else None
        made = 0
        for channel, list_id, app in targets:
            form = _form_for(channel)
            language, fallback = _pick_language(written[form], code, default_code)
            if language is None:
                reasons.append(_("nothing is written for email") if form == "email"
                               else _("nothing is written for Telegram and notifications"))
                continue
            key = f"{source}:p_{pid}:{channel}" + (f":{app.public_id}" if app is not None else "")
            rows.append(Message(
                person_id=pid, source="campaign", message_type="newsletter", campaign=campaign, list_id=list_id,
                channel=channel, language=language, used_fallback=fallback,
                sender=sender if channel == "email" else None, bot=bot if channel == "telegram" else None,
                push_app=app, state="queued", send_after=send_after, idempotency_key=key,
                subject_snapshot=(written[form][language].subject[:300] if form == "email" else ""),
            ))
            made += 1
        if not made:
            rows.append(Message(
                person_id=pid, source="campaign", message_type="newsletter", campaign=campaign,
                channel=channels[0] if channels else "email", language=code, state="skipped",
                error=_sentence(reasons) or _("Nothing to send on the channels this campaign uses"),
                idempotency_key=f"{source}:p_{pid}:skipped",
            ))
        if len(rows) >= INSERT_CHUNK:
            _insert(rows)
            rows = []
    _insert(rows)
    created = Message.objects.filter(campaign=campaign).exclude(state="skipped").count() - before

    if first_time:
        try:
            from channels.webhooks import post_campaign

            post_campaign(campaign)
        except Exception:  # noqa: BLE001 - a webhook must never stop a campaign
            log.exception("posting campaign %s to webhooks failed", campaign.pk)

    enqueue(_due(Message.objects.filter(campaign=campaign)))
    finish_campaign(campaign.pk)
    campaign.refresh_from_db()
    return created


def _sentence(reasons):
    seen = []
    for reason in reasons:
        if reason not in seen:
            seen.append(str(reason))
    text = "; ".join(seen)
    return text[:1].upper() + text[1:] if text else ""


def campaign_totals(campaign):
    """What is kept in Campaign.totals once message rows are deleted."""
    totals = {"messages": 0, "sent": 0, "opened": 0, "clicked": 0, "failed": 0, "skipped": 0, "waiting": 0,
              "people": 0, "by_channel": {}, "by_language": {}}
    rows = (Message.objects.filter(campaign=campaign).values("channel", "language", "state")
            .annotate(n=Count("id")))
    for row in rows:
        state, n = row["state"], row["n"]
        buckets = [totals]
        if state != "skipped":
            buckets.append(totals["by_channel"].setdefault(row["channel"], {"messages": 0, "sent": 0, "opened": 0, "clicked": 0, "failed": 0}))
            buckets.append(totals["by_language"].setdefault(row["language"], {"messages": 0, "sent": 0, "opened": 0, "clicked": 0, "failed": 0}))
        for bucket in buckets:
            if state == "skipped":
                bucket["skipped"] = bucket.get("skipped", 0) + n
                continue
            bucket["messages"] += n
            if state in Message.DELIVERED_STATES:
                bucket["sent"] += n
            if state in ("opened", "clicked"):
                bucket["opened"] += n
            if state == "clicked":
                bucket["clicked"] += n
            if state == "failed":
                bucket["failed"] += n
            if state in ("queued", "sending") and bucket is totals:
                bucket["waiting"] += n
    totals["people"] = (Message.objects.filter(campaign=campaign).exclude(state="skipped")
                        .values("person_id").distinct().count())
    return totals


def finish_campaign(campaign_id):
    """sending becomes sent when no message is queued or sending."""
    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if campaign is None or campaign.state not in ("sending", "sent"):
        return
    if (campaign.totals or {}).get("retained"):
        return
    if Message.objects.filter(campaign=campaign, state__in=("queued", "sending")).exists():
        return
    campaign.totals = campaign_totals(campaign)
    fields = ["totals", "updated_at"]
    if campaign.state == "sending":
        campaign.state = "sent"
        campaign.sent_at = timezone.now()
        fields += ["state", "sent_at"]
    campaign.save(update_fields=fields)


def pause_campaign(campaign):
    """Queued messages stop being picked up."""
    if campaign.state in ("sending", "scheduled"):
        campaign.state = "paused"
        campaign.save(update_fields=["state", "updated_at"])
    return campaign


def resume_campaign(campaign):
    """Continue where it stopped, without fanning out again."""
    if campaign.state != "paused":
        return campaign
    if campaign.started_at is None:
        # Paused before it started: it goes back to waiting for its time.
        campaign.state = "scheduled" if campaign.scheduled_at else "draft"
        campaign.save(update_fields=["state", "updated_at"])
        return campaign
    campaign.state = "sending"
    campaign.save(update_fields=["state", "updated_at"])
    enqueue(_due(Message.objects.filter(campaign=campaign)))
    finish_campaign(campaign.pk)
    campaign.refresh_from_db()
    return campaign


def cancel_campaign(campaign):
    """Stop for good: what has not gone out yet never will."""
    campaign.state = "cancelled"
    campaign.save(update_fields=["state", "updated_at"])
    Message.objects.filter(campaign=campaign, state="queued").update(
        state="skipped", error=_("The campaign was cancelled before this was sent"), updated_at=timezone.now())
    campaign.totals = campaign_totals(campaign)
    campaign.save(update_fields=["totals", "updated_at"])
    return campaign


def retry_message(message):
    """Staff pressed "Try again" in the Outbox."""
    if message.state not in ("failed", "skipped"):
        return message
    message.state = "queued"
    message.error = ""
    message.attempts = 0
    message.send_after = None
    message.sending_since = None
    message.requeued = False
    message.save(update_fields=["state", "error", "attempts", "send_after", "sending_since", "requeued", "updated_at"])
    enqueue(_due(Message.objects.filter(pk=message.pk)))
    message.refresh_from_db()
    return message


# --------------------------------------------------------------------------- app messages

def app_list_for(building):
    """The building's app list: its first one, or a new one named after the building."""
    lst = List.objects.filter(kind="app", building=building).order_by("pk").first()
    if lst is not None:
        return lst
    base = slugify(building.name) or slugify(building.kind) or f"building-{building.pk}"
    slug = candidate = f"{base[:70]}-app"
    n = 2
    while List.objects.filter(slug=candidate).exists():
        candidate = f"{slug}-{n}"
        n += 1
    return List.objects.create(name={Language.default_code(): building.name}, slug=candidate, kind="app",
                               building=building, on_signup_form=False)


def send_app_message(template, person, data=None, building=None, request=None):
    """Send one app message (message type service) to one person.

    request is the delivery.SendRequest of an API call, when there is one.
    Returns the Message rows, one per channel (or one skipped row saying why).
    """
    building = building or template.building
    sender = EmailSender.app_sender()
    bot = TelegramBot.default()
    lst = app_list_for(building) if building is not None else None
    reach = reachable_channels(person, bot)
    code = person.language_code
    default_code = Language.default_code()
    source = request.public_id if request is not None else f"{template.public_id}:{uuid.uuid4().hex[:12]}"
    common = dict(person=person, source="building", message_type="service", template=template, request=request,
                  building=building, list=lst, data=data or {})
    reasons, messages, has_any_form = [], [], False
    for channel in ("email", "telegram"):  # app messages have no push form
        form = _form_for(channel)
        written = _written(template, form)
        if not written:
            continue
        has_any_form = True
        if channel not in reach:
            reasons.append({"email": _("has no email address we can use"),
                            "telegram": _("is not connected to the Telegram bot")}[channel])
            continue
        if channel == "email" and sender is None:
            reasons.append(_("no sender for app messages is set up"))
            continue
        if lst is not None:
            sub = Subscription.objects.filter(person=person, list=lst, channel=channel).first()
            if sub is None:
                set_subscription(person, lst, channel, True)
            elif sub.state != "on":
                reasons.append({"email": _("turned email off for this app"),
                                "telegram": _("turned Telegram off for this app")}[channel])
                continue
        language, fallback = _pick_language(written, code, default_code, service=True)
        message, _created = Message.objects.get_or_create(
            idempotency_key=f"{source}:{person.public_id}:{channel}",
            defaults=dict(common, channel=channel, language=language, used_fallback=fallback,
                          sender=sender if channel == "email" else None, bot=bot if channel == "telegram" else None,
                          state="queued", subject_snapshot=(written[language].subject[:300] if form == "email" else "")),
        )
        messages.append(message)
    if not messages:
        if not has_any_form:
            reasons = [_("this template has nothing written yet")]
        message, _created = Message.objects.get_or_create(
            idempotency_key=f"{source}:{person.public_id}:skipped",
            defaults=dict(common, channel="email", language=code, state="skipped", error=_sentence(reasons)),
        )
        return [message]
    enqueue(_due(Message.objects.filter(pk__in=[m.pk for m in messages])))
    for message in messages:
        message.refresh_from_db()
    return messages


# --------------------------------------------------------------------------- quick messages

def send_quick_message(quick_message):
    """Send a quick message to its people and lists. Returns the number of messages created.

    People named directly and app lists get it as a service message. Campaign
    lists get it as a newsletter, so only subscriptions that are on count.
    An empty channels list means every channel each person uses.
    """
    qm = quick_message
    wanted = [c for c in CHANNELS if c in (qm.channels or CHANNELS)]
    bot = TelegramBot.default()
    app_sender = EmailSender.app_sender()
    news_sender = EmailSender.default_newsletter() or app_sender
    written = _written(qm, "notification")
    default_code = Language.default_code()
    source = qm.public_id

    plan = {}  # (person id, channel, push app id) -> target
    codes, reasons = {}, defaultdict(list)

    def add(pid, channel, lst, app, message_type):
        plan.setdefault((pid, channel, app.pk if app else None),
                        {"list": lst, "app": app, "type": message_type})

    for person in qm.people.select_related("language"):
        codes[person.pk] = person.language_code
        reach = reachable_channels(person, bot)
        for channel in wanted:
            if channel == "push":
                continue  # push goes to an app's audience, never to one named person
            if channel not in reach:
                reasons[person.pk].append({"email": _("has no email address we can use"),
                                           "telegram": _("is not connected to the Telegram bot")}[channel])
            elif channel == "email" and app_sender is None:
                reasons[person.pk].append(_("no sender for app messages is set up"))
            else:
                add(person.pk, channel, None, None, "service")

    for lst in qm.lists.all():
        newsletter = lst.kind == "campaign"
        sender = news_sender if newsletter else app_sender
        apps = list(lst.push_apps.all()) if newsletter else []
        for pid, code, targets, why in _reach([lst], wanted, sender, bot, apps):
            codes.setdefault(pid, code)
            if not targets:
                reasons[pid].extend(why)
            for channel, _list_id, app in targets:
                add(pid, channel, lst, app, lst.message_type)

    before = Message.objects.filter(quick_message=qm).exclude(state="skipped").count()
    rows, got_one = [], set()
    for (pid, channel, _app_id), target in plan.items():
        service = target["type"] == "service"
        language, fallback = _pick_language(written, codes[pid], default_code, service=service)
        if language is None:
            reasons[pid].append(_("nothing is written in a language we can send"))
            continue
        app, lst = target["app"], target["list"]
        got_one.add(pid)
        rows.append(Message(
            person_id=pid, source="quick", message_type=target["type"], quick_message=qm, list=lst,
            building=lst.building if (lst is not None and lst.kind == "app") else None,
            channel=channel, language=language, used_fallback=fallback,
            sender=((app_sender if service else news_sender) if channel == "email" else None),
            bot=bot if channel == "telegram" else None, push_app=app, state="queued",
            idempotency_key=f"{source}:p_{pid}:{channel}" + (f":{app.public_id}" if app is not None else ""),
        ))
    for pid in set(codes) - got_one:
        rows.append(Message(
            person_id=pid, source="quick", message_type="service", quick_message=qm,
            channel=wanted[0] if wanted else "email", language=codes[pid], state="skipped",
            error=_sentence(reasons[pid]) or _("Nothing to send on the channels chosen"),
            idempotency_key=f"{source}:p_{pid}:skipped",
        ))
    _insert(rows)
    created = Message.objects.filter(quick_message=qm).exclude(state="skipped").count() - before
    if qm.sent_at is None:
        qm.sent_at = timezone.now()
        qm.save(update_fields=["sent_at", "updated_at"])
    enqueue(_due(Message.objects.filter(quick_message=qm)))
    return created


def _quick_email_content(notification, org):
    """A quick message as a simple email: the notification text, with its button."""
    lines = [line for line in (notification.text or "").splitlines()]
    first = render.strip_marks(lines[0]).strip() if lines else ""
    rest = "\n".join(lines[1:]).strip()
    if first and rest:
        subject, body = first[:150], rest
    else:
        subject, body = org.name or _("A message for you"), notification.text
    blocks = [{"id": "q1", "type": "text", "text": body}]
    if notification.button_label and notification.button_url:
        blocks.append({"id": "q2", "type": "button", "text": notification.button_label, "href": notification.button_url})
    return Content(language=notification.language, form="email", subject=subject, mode="visual", blocks=blocks)


# --------------------------------------------------------------------------- system and test emails

def send_system_email(to_email, subject, html, text="", person=None):
    """One email right now from the app sender (confirmation emails, team invites).

    Returns None when there is no app sender. With a person the email is logged
    in the Outbox (source "system"); without one the returned Message is not saved.
    """
    sender = EmailSender.app_sender()
    if sender is None:
        return None
    message = Message(
        person=person, source="system", message_type="service", channel="email", sender=sender,
        language=person.language_code if person is not None else Language.default_code(),
        state="queued", subject_snapshot=(subject or "")[:300], idempotency_key=f"sys:{uuid.uuid4().hex}",
        data={"to": to_email, "html": html, "text": text},
    )
    if person is not None:
        message.save()
        deliver_batch([message.pk])
        message.refresh_from_db()
        return message
    ok, error = _send_now(sender, to_email, subject, html, text)
    message.state = "sent" if ok else "failed"
    message.error = error
    message.sent_at = timezone.now() if ok else None
    return message


def _send_now(sender, to_email, subject, html, text, headers=None):
    org = Organization.get()
    mime = email_channel.build_message(sender, to_email, subject, html, text, reply_to=org.reply_to, headers=headers)
    smtp = None
    try:
        smtp = email_channel.open_connection(sender)
        email_channel.send_mime(smtp, sender, to_email, mime)
    except email_channel.SendFailure as failure:
        return False, failure.reason
    except Exception as exc:  # noqa: BLE001 - the reason is shown to staff
        return False, email_channel.describe_error(exc)
    finally:
        email_channel.close_quietly(smtp)
    return True, ""


def send_test_email(sender, to_email, rendered):
    """Send a rendered email (content.render.RenderedEmail) as a test. Not logged."""
    return _send_now(sender, to_email, rendered.subject, rendered.html, rendered.text)


# --------------------------------------------------------------------------- step 6: send

class _Batch:
    """What one batch shares: the organization, open SMTP connections, tracked links, contents."""

    def __init__(self):
        self.org = Organization.get()
        self.now = timezone.now()
        self._smtp = {}
        self._smtp_down = {}
        self._links = {}
        self._contents = {}
        self.campaign_ids = set()

    def smtp(self, sender):
        if sender.pk in self._smtp_down:
            raise self._smtp_down[sender.pk]
        if sender.pk not in self._smtp:
            try:
                self._smtp[sender.pk] = email_channel.open_connection(sender)
            except Exception as exc:  # noqa: BLE001
                reason = email_channel.describe_error(exc)
                EmailSender.objects.filter(pk=sender.pk).update(state="error", last_error=reason)
                failure = email_channel.SendFailure(
                    _("Could not sign in to the mail server (%(reason)s)") % {"reason": reason}, temporary=True)
                self._smtp_down[sender.pk] = failure
                raise failure from exc
            if sender.state != "ok":
                EmailSender.objects.filter(pk=sender.pk).update(state="ok", last_error="")
        return self._smtp[sender.pk]

    def drop(self, sender):
        email_channel.close_quietly(self._smtp.pop(sender.pk, None))

    def close(self):
        for smtp in self._smtp.values():
            email_channel.close_quietly(smtp)
        self._smtp = {}

    def content(self, message, form):
        owner = message.campaign or message.template or message.quick_message
        if owner is None:
            return None
        key = (owner.__class__.__name__, owner.pk, message.language, form)
        if key not in self._contents:
            self._contents[key] = Content.objects.filter(language=message.language, form=form, **owner_filter(owner)).first()
        return self._contents[key]

    def tracker(self, message):
        """A function that rewrites links to the click redirect, or None when this message is not tracked."""
        if message.message_type != "newsletter" or not message.campaign_id or not self.org.tracking_enabled:
            return None
        own = (settings.PUBLIC_URL + "/u/", settings.PUBLIC_URL + "/p/", settings.PUBLIC_URL + "/t/")

        def track(url):
            if not url or not url.startswith(("http://", "https://")) or url.startswith(own):
                return url
            key = (message.campaign_id, url)
            if key not in self._links:
                self._links[key] = TrackedLink.objects.get_or_create(campaign_id=message.campaign_id, url=url)[0]
            return f"{settings.PUBLIC_URL}/t/c/{self._links[key].token}?m={message.token}"

        return track


def deliver_batch(message_ids):
    """Lock the rows, mark them sending, render per person, send, record. Returns how many were handled."""
    now = timezone.now()
    with transaction.atomic():
        claimed = list(_due(Message.objects.select_for_update(skip_locked=True).filter(pk__in=list(message_ids)), now)
                       .values_list("pk", flat=True))
        if not claimed:
            return 0
        Message.objects.filter(pk__in=claimed).update(state="sending", sending_since=now, updated_at=now)
    messages = list(Message.objects.filter(pk__in=claimed).select_related(
        "person", "person__language", "campaign", "template", "quick_message", "sender", "bot", "push_app", "list",
        "request").order_by("pk"))
    batch = _Batch()
    try:
        push_groups = defaultdict(list)
        for message in messages:
            if message.campaign_id:
                batch.campaign_ids.add(message.campaign_id)
            if message.channel == "push":
                push_groups[(message.push_app_id, _source_id(message))].append(message)
                continue
            try:
                if message.channel == "email":
                    _deliver_email(message, batch)
                else:
                    _deliver_telegram(message, batch)
            except Exception as exc:  # noqa: BLE001 - one bad message must not strand the rest in "sending"
                log.exception("message %s could not be sent", message.pk)
                _fail(message, _("Post Office hit a problem preparing this message (%(error)s)") % {"error": exc})
        for group in push_groups.values():
            for start in range(0, len(group), limits.PUSH_BATCH):
                chunk = group[start:start + limits.PUSH_BATCH]
                try:
                    _deliver_push(chunk, batch)
                except Exception as exc:  # noqa: BLE001
                    log.exception("push batch could not be sent")
                    for message in chunk:
                        if message.state == "sending":
                            _fail(message, _("Post Office hit a problem preparing this message (%(error)s)") % {"error": exc})
    finally:
        batch.close()
    for campaign_id in batch.campaign_ids:
        finish_campaign(campaign_id)
    return len(messages)


def _source_id(message):
    if message.campaign_id:
        return message.campaign.public_id
    if message.request_id:
        return message.request.public_id
    if message.quick_message_id:
        return message.quick_message.public_id
    if message.template_id:
        return message.template.public_id
    return "sys"


def _save(message, *fields):
    message.save(update_fields=[*fields, "updated_at"])


def _sent(message, subject=None, warnings=None):
    message.state = "sent"
    message.sent_at = timezone.now()
    message.sending_since = None
    message.attempts += 1
    message.error = ""
    if subject:
        message.subject_snapshot = subject[:300]
    if warnings:
        message.warnings = [str(w) for w in warnings][:20]
    _save(message, "state", "sent_at", "sending_since", "attempts", "error", "subject_snapshot", "warnings")


def _fail(message, reason):
    message.state = "failed"
    message.error = str(reason)
    message.sending_since = None
    message.attempts += 1
    _save(message, "state", "error", "sending_since", "attempts")


def _retry(message, reason, wait_seconds=None):
    """A temporary failure: try again after 1, 5, 30 and 120 minutes, then give up."""
    if message.attempts >= len(RETRY_MINUTES):
        _fail(message, _("%(reason)s. Tried %(n)s times.") % {"reason": reason, "n": message.attempts + 1})
        return
    wait = RETRY_MINUTES[message.attempts] * 60
    if wait_seconds:
        wait = max(wait, int(wait_seconds))
    message.attempts += 1
    message.state = "queued"
    message.error = str(reason)
    message.sending_since = None
    message.send_after = timezone.now() + datetime.timedelta(seconds=wait)
    _save(message, "state", "error", "sending_since", "attempts", "send_after")


def _back_to_queue(message, send_after=None):
    """Not sent and not a failure: it simply waits (a limit was reached)."""
    message.state = "queued"
    message.sending_since = None
    if send_after is not None:
        message.send_after = send_after
    _save(message, "state", "sending_since", "send_after")


def _render_email(message, batch):
    """Return (to_email, subject, html, text, headers, warnings)."""
    person = message.person
    if message.source == "system":
        data = message.data or {}
        return (data.get("to") or (person.email if person else ""), message.subject_snapshot, data.get("html", ""),
                data.get("text", ""), {}, [])
    org = batch.org
    if message.quick_message_id:
        notification = batch.content(message, "notification")
        content = _quick_email_content(notification, org) if notification is not None else None
    else:
        content = batch.content(message, "email")
    if content is None:
        raise email_channel.SendFailure(_("There is nothing written to send in this language any more"))
    links = links_for(person, message)
    newsletter = message.message_type == "newsletter"
    if newsletter:
        telegram_link = _footer_telegram_link(person, org)
        if telegram_link:
            links["telegram"] = telegram_link
    context = render.build_context(person, message.data, links)
    track = batch.tracker(message)
    pixel = f"{settings.PUBLIC_URL}/t/o/{message.token}.gif" if track else ""
    list_name = message.list.display_name(message.language) if (newsletter and message.list_id) else None
    out = render.render_email(content, context, message_type=message.message_type, org=org, track=track,
                              pixel_url=pixel, list_name=list_name)
    headers = {}
    if newsletter:
        mailto = org.reply_to or message.sender.from_email
        headers["List-Unsubscribe"] = f"<{links['unsubscribe']}>, <mailto:{mailto}?subject=unsubscribe>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return person.email, out.subject, out.html, out.text, headers, out.warnings


def _deliver_email(message, batch):
    sender, person = message.sender, message.person
    if sender is None:
        return _fail(message, _("The email sender this was going out from has been removed"))
    if message.source != "system":
        if person is None or not person.email:
            return _fail(message, _("This person has no email address"))
        if person.email_state != "ok":
            return _fail(message, _("Email to this address bounced earlier, so nothing more is sent to it"))
    if not limits.reserve_daily(sender, batch.org):
        # Over today's limit: it waits until 00:05 tomorrow in the organization's time zone.
        return _back_to_queue(message, limits.next_day_start(batch.org))
    if not limits.take_email_token(sender):
        limits.release_daily(sender, batch.org)
        return _back_to_queue(message)
    try:
        to_email, subject, html, text, headers, warnings = _render_email(message, batch)
        mime = email_channel.build_message(sender, to_email, subject, html, text, reply_to=batch.org.reply_to,
                                           headers=headers)
        email_channel.send_mime(batch.smtp(sender), sender, to_email, mime)
    except email_channel.SendFailure as failure:
        limits.release_daily(sender, batch.org)
        if failure.connection_lost:
            batch.drop(sender)
        if failure.bounced and person is not None and message.source != "system":
            Person.objects.filter(pk=person.pk).update(email_state="bounced", updated_at=timezone.now())
            person.email_state = "bounced"
        if failure.temporary:
            return _retry(message, failure.reason)
        return _fail(message, failure.reason)
    except Exception:
        limits.release_daily(sender, batch.org)
        raise
    _sent(message, subject, warnings)


def _deliver_telegram(message, batch):
    bot, person = message.bot, message.person
    if bot is None:
        return _fail(message, _("The Telegram bot this was going out from has been removed"))
    link = TelegramLink.objects.filter(person=person, bot=bot, blocked_at__isnull=True).first() if person else None
    if link is None:
        return _fail(message, _("This person is no longer connected to the Telegram bot"))
    content = batch.content(message, "notification")
    if content is None:
        return _fail(message, _("There is nothing written to send in this language any more"))
    if not limits.take_telegram_token(bot):
        return _back_to_queue(message)
    context = render.build_context(person, message.data, links_for(person, message))
    out = render.render_notification(content, context, track=batch.tracker(message))
    button = (out.button_label, out.button_url) if (out.button_label and out.button_url) else None
    image = out.image_url if (out.image_url and len(out.telegram_html) <= render.TELEGRAM_CAPTION_LIMIT) else None
    try:
        telegram_channel.TelegramClient(bot).send(link.chat_id, out.telegram_html[:render.TELEGRAM_LIMIT],
                                                  button=button, image_url=image)
    except telegram_channel.TelegramError as error:
        if error.blocked:
            link.blocked_at = timezone.now()
            link.save(update_fields=["blocked_at", "updated_at"])
        if error.permanent:
            return _fail(message, error.for_staff())
        return _retry(message, error.for_staff(), error.retry_after)
    _sent(message, warnings=out.warnings)


def _deliver_push(messages, batch):
    """One signed request for up to 500 notifications to one app."""
    app = messages[0].push_app
    if app is None:
        for message in messages:
            _fail(message, _("The app this notification was going to is no longer connected"))
        return
    if not limits.push_lock(app):
        for message in messages:
            _back_to_queue(message)  # another request to this app is running; one at a time
        return
    try:
        external = {}
        for person_id, external_id in (PushMember.objects.filter(push_app=app, person_id__in=[m.person_id for m in messages])
                                       .order_by("pk").values_list("person_id", "external_id")):
            external.setdefault(person_id, external_id)
        items, by_external = [], {}
        for message in messages:
            external_id = external.get(message.person_id)
            if external_id is None:
                _fail(message, _("This person has turned notifications off in the app"))
                continue
            content = batch.content(message, "notification")
            if content is None:
                _fail(message, _("There is nothing written to send in this language any more"))
                continue
            context = render.build_context(message.person, message.data, links_for(message.person, message))
            out = render.render_notification(content, context, track=batch.tracker(message))
            items.append({"external_id": external_id, "title": batch.org.name, "body": out.push_body, "url": out.button_url})
            by_external[external_id] = message
        if not items:
            return
        key = f"{_source_id(messages[0])}:push:{min(m.pk for m in by_external.values())}"
        try:
            accepted, rejected = push_channel.send_batch(app, items, key)
        except push_channel.PushError as error:
            for message in by_external.values():
                if error.temporary:
                    _retry(message, error.reason)
                else:
                    _fail(message, error.reason)
            return
        for external_id, message in by_external.items():
            if external_id in accepted:
                _sent(message)
            elif external_id in rejected:
                _fail(message, _("The app could not deliver this notification"))
            else:
                _fail(message, _("The app did not say whether it delivered this notification"))
    finally:
        limits.push_unlock(app)


# --------------------------------------------------------------------------- crash safety and retention

def recover_stuck(now=None):
    """Messages stuck in sending for more than 10 minutes (a worker died mid-batch).

    Service messages are re-queued once. Campaign messages are marked failed so
    nobody gets a newsletter twice; staff can retry them from the Outbox.
    Returns (requeued, failed).
    """
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(minutes=STUCK_AFTER_MINUTES)
    stuck = Message.objects.filter(state="sending", sending_since__lt=cutoff)
    campaign_ids = set(stuck.exclude(campaign__isnull=True).values_list("campaign_id", flat=True))
    retry_ids = list(stuck.filter(message_type="service", campaign__isnull=True, requeued=False).values_list("pk", flat=True))
    requeued = Message.objects.filter(pk__in=retry_ids, state="sending").update(
        state="queued", requeued=True, sending_since=None, send_after=None, updated_at=now)
    failed = Message.objects.filter(state="sending", sending_since__lt=cutoff).update(
        state="failed", error=INTERRUPTED, sending_since=None, updated_at=now)
    if retry_ids:
        enqueue(_due(Message.objects.filter(pk__in=retry_ids)))
    for campaign_id in campaign_ids:
        finish_campaign(campaign_id)
    return requeued, failed


def apply_retention(now=None):
    """Delete message rows older than 180 days, keeping campaign totals. Returns how many rows went."""
    from .models import SendRequest

    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=RETENTION_DAYS)
    old = Message.objects.filter(created_at__lt=cutoff)
    for campaign in Campaign.objects.filter(pk__in=old.exclude(campaign__isnull=True).values("campaign_id").distinct()):
        if not (campaign.totals or {}).get("retained"):
            totals = campaign_totals(campaign)
            from campaigns.reports import report_extras  # unsubscribed, failure reasons, links, 48-hour opens

            totals.update(report_extras(campaign))
            totals["retained"] = True
            campaign.totals = totals
            campaign.save(update_fields=["totals", "updated_at"])
    deleted = 0
    while True:
        ids = list(old.values_list("pk", flat=True)[:2000])
        if not ids:
            break
        Message.objects.filter(pk__in=ids).delete()
        deleted += len(ids)
    SendRequest.objects.filter(created_at__lt=cutoff, messages__isnull=True).delete()
    return deleted


def send_due(limit=5000):
    """Queue everything whose time has come (retries, staggered and held messages)."""
    ids = list(_due(Message.objects.all()).order_by("send_after", "pk").values_list("pk", flat=True)[:limit])
    if not ids:
        return 0
    return enqueue(Message.objects.filter(pk__in=ids))
