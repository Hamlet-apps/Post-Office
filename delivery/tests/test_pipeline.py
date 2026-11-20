import datetime
import hashlib
import hmac
import json
import smtplib
from unittest import mock

import httpx
import pytest
from django.utils import timezone

from campaigns.models import Campaign
from channels.telegram import TelegramError
from content.models import Content, QuickMessage
from delivery import limits, pipeline
from delivery.models import Message
from delivery.tasks import apply_retention, recover_stuck_messages, send_due_messages
from people.models import Person, Subscription
from people.services import set_subscription

from . import helpers as h

pytestmark = pytest.mark.django_db


@pytest.fixture
def newsletter(org):
    return h.make_list()


def _three_subscribers(lst):
    people = [h.make_person(f"person{n}@example.com", f"Person{n}") for n in (1, 2, 3)]
    for person in people:
        h.subscribe(person, lst)
    return people


# --------------------------------------------------------------------------- fan-out

def test_campaign_reaches_each_subscriber_once_even_when_fanned_out_twice(newsletter, smtp):
    _three_subscribers(newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    assert pipeline.send_campaign(campaign) == 3
    assert pipeline.send_campaign(campaign) == 0  # the unique idempotency key makes a repeat harmless
    assert len(smtp.sent) == 3
    assert smtp.connections == 1  # one SMTP connection per sender within a batch
    campaign.refresh_from_db()
    assert campaign.state == "sent" and campaign.sent_at and campaign.totals["sent"] == 3
    keys = set(Message.objects.values_list("idempotency_key", flat=True))
    person = Person.objects.get(email="person1@example.com")
    assert f"{campaign.public_id}:{person.public_id}:email" in keys


def test_newsletter_email_has_unsubscribe_headers_text_part_and_tracking(newsletter, smtp, org):
    person = h.make_person()
    h.subscribe(person, newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    pipeline.send_campaign(campaign)
    to, mime = smtp.sent[0]
    message = Message.objects.get()
    assert to == "olena.k@example.com" and mime["Subject"] == "Coat drive (en)"
    assert f"<https://post.maplegrove.example/u/{message.token}>" in mime["List-Unsubscribe"]
    assert "<mailto:hello@maplegrove.example?subject=unsubscribe>" in mime["List-Unsubscribe"]
    assert mime["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert mime["Reply-To"] == "hello@maplegrove.example"
    assert "Hi Olena" in smtp.text() and "12 Alder Street" in smtp.text()
    html = smtp.html()
    assert f"/t/o/{message.token}.gif" in html
    assert "https://post.maplegrove.example/t/c/" in html and "https://maplegrove.example/calendar" not in html


def test_tracking_can_be_switched_off(newsletter, smtp, org):
    org.tracking_enabled = False
    org.save()
    h.subscribe(h.make_person(), newsletter)
    pipeline.send_campaign(h.make_campaign([newsletter], sender=h.make_sender()))
    assert "/t/o/" not in smtp.html() and "https://maplegrove.example/calendar" in smtp.html()


def test_unwritten_language_gets_the_default_and_is_marked(newsletter, smtp):
    ukrainian = h.make_person("taras@example.com", "Taras", language="uk")
    h.subscribe(ukrainian, newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender(), languages=("en",))
    pipeline.send_campaign(campaign)
    message = Message.objects.get(person=ukrainian)
    assert message.used_fallback is True and message.language == "en" and message.state == "sent"
    assert smtp.sent[0][1]["Subject"] == "Coat drive (en)"


def test_written_language_is_used_without_fallback(newsletter, smtp):
    ukrainian = h.make_person("taras@example.com", "Taras", language="uk")
    h.subscribe(ukrainian, newsletter)
    pipeline.send_campaign(h.make_campaign([newsletter], sender=h.make_sender(), languages=("en", "uk")))
    message = Message.objects.get(person=ukrainian)
    assert message.used_fallback is False and message.language == "uk"
    assert smtp.sent[0][1]["Subject"] == "Coat drive (uk)"


def test_unconfirmed_subscriber_gets_no_campaign(newsletter, smtp):
    waiting = h.make_person("waiting@example.com", "Waiting")
    h.subscribe(waiting, newsletter, unconfirmed=True)
    confirmed = h.make_person()
    h.subscribe(confirmed, newsletter)
    assert pipeline.send_campaign(h.make_campaign([newsletter], sender=h.make_sender())) == 1
    assert [to for to, _m in smtp.sent] == ["olena.k@example.com"]
    assert not Message.objects.filter(person=waiting).exists()


def test_person_with_email_and_telegram_gets_both(newsletter, smtp):
    bot = h.make_bot()
    person = h.make_person()
    h.subscribe(person, newsletter, "email")
    h.subscribe(person, newsletter, "telegram")
    h.connect_telegram(person, bot, chat_id=555)
    campaign = h.make_campaign([newsletter], channels=("email", "telegram"), sender=h.make_sender(), bot=bot)
    with mock.patch("channels.telegram.TelegramClient") as client:
        assert pipeline.send_campaign(campaign) == 2
    assert len(smtp.sent) == 1
    client.return_value.send.assert_called_once()
    args, kwargs = client.return_value.send.call_args
    assert args[0] == 555 and args[1].startswith("<b>Coat drive</b>")
    assert kwargs["button"][0] == "Details" and "/t/c/" in kwargs["button"][1]  # link buttons use the click redirect
    assert set(Message.objects.values_list("channel", "state")) == {("email", "sent"), ("telegram", "sent")}


def test_person_reachable_on_no_channel_gets_one_skipped_row_saying_why(newsletter, smtp):
    bot = h.make_bot()
    person = h.make_person(email=None)
    h.subscribe(person, newsletter, "telegram")  # subscribed, but never connected the bot
    campaign = h.make_campaign([newsletter], channels=("email", "telegram"), sender=h.make_sender(), bot=bot)
    assert pipeline.send_campaign(campaign) == 0
    message = Message.objects.get()
    assert message.state == "skipped" and "not connected to the Telegram bot" in message.error
    assert smtp.sent == []


def test_blocked_bot_marks_the_link_and_fails_with_a_readable_reason(newsletter):
    bot = h.make_bot()
    person = h.make_person()
    h.subscribe(person, newsletter, "telegram")
    link = h.connect_telegram(person, bot)
    campaign = h.make_campaign([newsletter], channels=("telegram",), bot=bot)
    with mock.patch("channels.telegram.TelegramClient") as client:
        client.return_value.send.side_effect = TelegramError(403, "Forbidden: bot was blocked by the user")
        pipeline.send_campaign(campaign)
    message = Message.objects.get()
    link.refresh_from_db()
    assert link.blocked_at is not None
    assert message.state == "failed" and message.error == "Telegram says this person has blocked the bot"
    campaign.refresh_from_db()
    assert campaign.state == "sent" and campaign.totals["failed"] == 1


def test_telegram_429_is_retried_later(newsletter):
    bot = h.make_bot()
    person = h.make_person()
    h.subscribe(person, newsletter, "telegram")
    h.connect_telegram(person, bot)
    campaign = h.make_campaign([newsletter], channels=("telegram",), bot=bot)
    with mock.patch("channels.telegram.TelegramClient") as client:
        client.return_value.send.side_effect = TelegramError(429, "Too Many Requests", retry_after=3)
        pipeline.send_campaign(campaign)
    message = Message.objects.get()
    assert message.state == "queued" and message.attempts == 1 and message.send_after > timezone.now()
    campaign.refresh_from_db()
    assert campaign.state == "sending"


def test_push_send_posts_one_signed_batch_to_the_apps_endpoint(newsletter, org):
    building, _key = h.make_building()
    app = h.make_push_app(building, newsletter)
    people = [h.make_person(f"p{n}@example.com", f"P{n}") for n in (1, 2)]
    for n, person in enumerate(people, start=1):
        h.subscribe(person, newsletter, "push")
        h.allow_push(person, app, f"cust_{n}")
    campaign = h.make_campaign([newsletter], channels=("push",))
    campaign.push_apps.set([app])
    answer = httpx.Response(200, json={"accepted": ["cust_1"], "rejected": ["cust_2"]})
    with mock.patch("httpx.post", return_value=answer) as post:
        assert pipeline.send_campaign(campaign) == 2
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://pavilion.maplegrove.example/api/notifications"
    body = kwargs["content"]
    expected = "sha256=" + hmac.new(b"shared-secret", body, hashlib.sha256).hexdigest()
    assert kwargs["headers"]["X-PostOffice-Signature"] == expected
    payload = json.loads(body)
    assert payload["audience"] == "offers-and-news" and payload["idempotency_key"].startswith(f"{campaign.public_id}:push")
    assert [m["external_id"] for m in payload["messages"]] == ["cust_1", "cust_2"]
    assert payload["messages"][0]["title"] == "Maple Grove Community Centre"
    assert payload["messages"][0]["body"] == "Coat drive this Saturday (en)"
    states = dict(Message.objects.values_list("person__email", "state"))
    assert states == {"p1@example.com": "sent", "p2@example.com": "failed"}


def test_push_endpoint_error_is_retried(newsletter, org):
    building, _key = h.make_building()
    app = h.make_push_app(building, newsletter)
    person = h.make_person()
    h.subscribe(person, newsletter, "push")
    h.allow_push(person, app)
    campaign = h.make_campaign([newsletter], channels=("push",))
    campaign.push_apps.set([app])
    with mock.patch("httpx.post", return_value=httpx.Response(503)):
        pipeline.send_campaign(campaign)
    message = Message.objects.get()
    assert message.state == "queued" and message.attempts == 1


# --------------------------------------------------------------------------- limits and retries

def test_daily_limit_of_two_holds_the_third_message_until_the_next_day(newsletter, smtp, org):
    _three_subscribers(newsletter)
    sender = h.make_sender(daily_limit=2)
    campaign = h.make_campaign([newsletter], sender=sender)
    pipeline.send_campaign(campaign)
    assert len(smtp.sent) == 2
    held = Message.objects.get(state="queued")
    local = held.send_after.astimezone(org.tz)
    tomorrow = (timezone.now().astimezone(org.tz) + datetime.timedelta(days=1)).date()
    assert (local.date(), local.hour, local.minute) == (tomorrow, 0, 5)
    assert limits.sender_status(sender) == {"sent_today": 2, "waiting": 1}
    campaign.refresh_from_db()
    assert campaign.state == "sending"
    # The beat task does not pick it up before its time...
    send_due_messages()
    assert len(smtp.sent) == 2
    # ...and sends it once the next day has come.
    Message.objects.filter(pk=held.pk).update(send_after=timezone.now() - datetime.timedelta(seconds=1))
    from django.core.cache import cache
    cache.clear()  # a new day: a new counter
    Message.objects.filter(state="sent").update(sent_at=timezone.now() - datetime.timedelta(days=1))
    send_due_messages()
    assert len(smtp.sent) == 3
    campaign.refresh_from_db()
    assert campaign.state == "sent"


def test_temporary_smtp_failure_retries_after_1_5_30_120_minutes_then_fails(newsletter, smtp):
    h.subscribe(h.make_person(), newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    smtp.fail_with = smtplib.SMTPDataError(451, b"4.3.0 Try again later")
    pipeline.send_campaign(campaign)
    message = Message.objects.get()
    for attempt, minutes in enumerate((1, 5, 30, 120), start=1):
        message.refresh_from_db()
        assert message.state == "queued" and message.attempts == attempt
        wait = (message.send_after - timezone.now()).total_seconds() / 60
        assert minutes - 0.5 < wait <= minutes
        Message.objects.filter(pk=message.pk).update(send_after=timezone.now())
        send_due_messages()
    message.refresh_from_db()
    assert message.state == "failed" and "451" in message.error
    assert Person.objects.get().email_state == "ok"


def test_refused_recipient_fails_at_once_and_marks_the_address_bounced(newsletter, smtp):
    person = h.make_person()
    h.subscribe(person, newsletter)
    smtp.fail_with = smtplib.SMTPRecipientsRefused({"olena.k@example.com": (550, b"5.1.1 No such user")})
    pipeline.send_campaign(h.make_campaign([newsletter], sender=h.make_sender()))
    message = Message.objects.get()
    person.refresh_from_db()
    assert message.state == "failed" and "550" in message.error and person.email_state == "bounced"
    # No further email to that person until staff clear it.
    smtp.fail_with = None
    second = h.make_campaign([newsletter], sender=message.sender, name="January news")
    assert pipeline.send_campaign(second) == 0
    assert smtp.sent == [] and Message.objects.get(campaign=second).state == "skipped"


def test_connection_failure_is_temporary(newsletter):
    h.subscribe(h.make_person(), newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    with mock.patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
        pipeline.send_campaign(campaign)
    message = Message.objects.get()
    assert message.state == "queued" and message.attempts == 1 and "mail server" in message.error
    message.sender.refresh_from_db()
    assert message.sender.state == "error"


# --------------------------------------------------------------------------- crash safety

def test_killed_worker_does_not_resend_a_campaign_message(newsletter, smtp):
    person = h.make_person()
    h.subscribe(person, newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    campaign.state = "sending"
    campaign.started_at = timezone.now()
    campaign.save()
    stuck = Message.objects.create(
        person=person, source="campaign", message_type="newsletter", campaign=campaign, channel="email",
        sender=campaign.sender, language="en", state="sending", idempotency_key="cmp:stuck",
        sending_since=timezone.now() - datetime.timedelta(minutes=11))
    fresh = Message.objects.create(
        person=person, source="campaign", message_type="newsletter", campaign=campaign, channel="telegram",
        language="en", state="sending", idempotency_key="cmp:fresh", sending_since=timezone.now() - datetime.timedelta(minutes=2))
    recover_stuck_messages()
    send_due_messages()
    stuck.refresh_from_db()
    fresh.refresh_from_db()
    assert stuck.state == "failed" and stuck.error == "interrupted, not retried"
    assert fresh.state == "sending"  # only 2 minutes: the worker may still be on it
    assert smtp.sent == []
    # Staff can retry it from the Outbox.
    pipeline.retry_message(stuck)
    stuck.refresh_from_db()
    assert stuck.state == "sent" and len(smtp.sent) == 1


def test_stuck_service_message_is_requeued_once(org, smtp):
    building, _key = h.make_building()
    template = h.make_template(building, notification=False)
    person = h.make_person()
    h.make_sender("app")
    message = pipeline.send_app_message(template, person, {"order": {"number": "#0142", "ready_time": "6:40 PM"}})[0]
    assert message.state == "sent" and len(smtp.sent) == 1
    long_ago = timezone.now() - datetime.timedelta(minutes=11)
    Message.objects.filter(pk=message.pk).update(state="sending", sending_since=long_ago)
    recover_stuck_messages()
    message.refresh_from_db()
    assert message.requeued is True and message.state == "sent" and len(smtp.sent) == 2
    Message.objects.filter(pk=message.pk).update(state="sending", sending_since=long_ago)
    recover_stuck_messages()
    message.refresh_from_db()
    assert message.state == "failed" and len(smtp.sent) == 2


# --------------------------------------------------------------------------- campaign state

def test_spread_minutes_staggers_and_pause_and_resume_do_not_fan_out_again(newsletter, smtp):
    _three_subscribers(newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender(), spread_minutes=30)
    start = timezone.now()
    pipeline.send_campaign(campaign)
    assert len(smtp.sent) == 1  # the first goes now, the others are spread over the half hour
    offsets = sorted(round((m.send_after - start).total_seconds() / 60) for m in Message.objects.all())
    assert offsets == [0, 10, 20]
    pipeline.pause_campaign(campaign)
    Message.objects.filter(state="queued").update(send_after=timezone.now())
    send_due_messages()
    assert len(smtp.sent) == 1  # paused: queued messages are not picked up
    campaign.refresh_from_db()
    assert campaign.state == "paused"
    pipeline.resume_campaign(campaign)
    assert len(smtp.sent) == 3 and Message.objects.count() == 3
    campaign.refresh_from_db()
    assert campaign.state == "sent"


def test_cancel_stops_what_has_not_gone_out(newsletter, smtp):
    _three_subscribers(newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender(), spread_minutes=30)
    pipeline.send_campaign(campaign)
    pipeline.cancel_campaign(campaign)
    Message.objects.filter(state="queued").update(send_after=timezone.now())
    send_due_messages()
    campaign.refresh_from_db()
    assert campaign.state == "cancelled" and len(smtp.sent) == 1
    assert Message.objects.filter(state="skipped").count() == 2


def test_audience_counts_without_creating_rows(newsletter):
    bot = h.make_bot()
    sender = h.make_sender()
    both = h.make_person()
    h.subscribe(both, newsletter, "email")
    h.subscribe(both, newsletter, "telegram")
    h.connect_telegram(both, bot)
    email_only = h.make_person("marc@example.com", "Marc", language="fr")
    h.subscribe(email_only, newsletter, "email")
    no_consent = h.make_person("old@example.com", "Old")
    Subscription.objects.create(person=no_consent, list=newsletter, channel="email", state="on")
    unconfirmed = h.make_person("new@example.com", "New")
    h.subscribe(unconfirmed, newsletter, unconfirmed=True)
    result = pipeline.audience([newsletter], ["email", "telegram"], sender=sender, bot=bot)
    assert result == {"people": 3, "email": 3, "telegram": 1, "push": 0, "by_language": {"en": 2, "fr": 1},
                      "without_consent": 1}
    assert Message.objects.count() == 0


# --------------------------------------------------------------------------- app, quick and system messages

def test_app_message_goes_out_on_email_and_telegram_and_joins_the_app_list(org, smtp):
    building, _key = h.make_building()
    template = h.make_template(building)
    h.make_sender("app")
    bot = h.make_bot()
    person = h.make_person(language="uk")
    h.connect_telegram(person, bot)
    with mock.patch("channels.telegram.TelegramClient") as client:
        messages = pipeline.send_app_message(template, person, {"order": {"number": "#0142", "ready_time": "6:40 PM"}})
    assert {m.channel: m.state for m in messages} == {"email": "sent", "telegram": "sent"}
    assert all(m.used_fallback and m.language == "en" and m.message_type == "service" for m in messages)
    mime = smtp.sent[0][1]
    assert mime["Subject"] == "Order #0142 (Olena)" and "List-Unsubscribe" not in mime
    assert "Ready at 6:40 PM." in smtp.text() and "/t/o/" not in smtp.html()  # service messages are not tracked
    assert client.return_value.send.call_args[0][1] == "Order #0142 is in."
    app_list = building.lists.get(kind="app")
    assert app_list.slug == "pavilion-app"
    assert set(Subscription.objects.filter(person=person, list=app_list, state="on").values_list("channel", flat=True)) == {"email", "telegram"}


def test_app_message_respects_a_channel_the_person_turned_off(org, smtp):
    building, _key = h.make_building()
    template = h.make_template(building, notification=False)
    h.make_sender("app")
    person = h.make_person()
    set_subscription(person, pipeline.app_list_for(building), "email", False)
    messages = pipeline.send_app_message(template, person, {})
    assert len(messages) == 1 and messages[0].state == "skipped" and "turned email off" in messages[0].error.lower()
    assert smtp.sent == []


def test_quick_message_follows_consent_for_campaign_lists_and_not_for_people(newsletter, smtp):
    h.make_sender("app")
    h.make_sender("newsletter")
    subscribed = h.make_person("sub@example.com", "Sub")
    h.subscribe(subscribed, newsletter)
    unsubscribed = h.make_person("off@example.com", "Off")
    h.subscribe(unsubscribed, newsletter)
    set_subscription(unsubscribed, newsletter, "email", False)
    direct = h.make_person("direct@example.com", "Direct")
    quick = QuickMessage.objects.create(channels=[])
    quick.lists.set([newsletter])
    quick.people.set([direct])
    Content.objects.create(quick_message=quick, language="en", form="notification", text="Hall closed today\nThe boiler is being fixed.")
    assert pipeline.send_quick_message(quick) == 2
    sent = {to: mime for to, mime in smtp.sent}
    assert set(sent) == {"sub@example.com", "direct@example.com"}
    assert sent["sub@example.com"]["Subject"] == "Hall closed today" and "List-Unsubscribe" in sent["sub@example.com"]
    assert "List-Unsubscribe" not in sent["direct@example.com"]
    types = dict(Message.objects.filter(state="sent").values_list("person__email", "message_type"))
    assert types == {"sub@example.com": "newsletter", "direct@example.com": "service"}
    quick.refresh_from_db()
    assert quick.sent_at is not None


def test_system_email_is_logged_for_a_person_and_none_without_a_sender(org, smtp):
    assert pipeline.send_system_email("olena.k@example.com", "Confirm", "<p>Hi</p>", "Hi") is None
    h.make_sender("app")
    person = h.make_person()
    message = pipeline.send_system_email(person.email, "Please confirm", "<p>Press the button</p>", "Press the button", person=person)
    assert message.pk and message.source == "system" and message.state == "sent"
    assert smtp.sent[0][0] == person.email and smtp.sent[0][1]["Subject"] == "Please confirm"
    invite = pipeline.send_system_email("new.teammate@example.com", "You are invited", "<p>Join</p>")
    assert invite.pk is None and invite.state == "sent" and Message.objects.count() == 1


def test_test_email_is_not_logged(org, smtp):
    from content.render import RenderedEmail

    sender = h.make_sender()
    ok, error = pipeline.send_test_email(sender, "andrew@example.com", RenderedEmail("Test", "<p>Test</p>", "Test"))
    assert ok and error == "" and smtp.sent[0][0] == "andrew@example.com" and Message.objects.count() == 0
    smtp.fail_with = smtplib.SMTPDataError(554, b"Rejected")
    ok, error = pipeline.send_test_email(sender, "andrew@example.com", RenderedEmail("Test", "<p>Test</p>", "Test"))
    assert not ok and "554" in error


# --------------------------------------------------------------------------- retention

def test_retention_deletes_old_rows_and_keeps_campaign_totals(newsletter, smtp):
    _three_subscribers(newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    pipeline.send_campaign(campaign)
    recent = h.make_campaign([newsletter], sender=campaign.sender, name="Recent")
    pipeline.send_campaign(recent)
    Message.objects.filter(campaign=campaign).update(created_at=timezone.now() - datetime.timedelta(days=181))
    Campaign.objects.filter(pk=campaign.pk).update(totals={})
    assert apply_retention() == 3
    campaign.refresh_from_db()
    assert campaign.totals["sent"] == 3 and campaign.totals["retained"] is True
    assert Message.objects.filter(campaign=campaign).count() == 0 and Message.objects.filter(campaign=recent).count() == 3
