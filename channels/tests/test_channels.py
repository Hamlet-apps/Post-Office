import hashlib
import hmac
import json
import smtplib
from unittest import mock

import httpx
import pytest
from django.test import Client

from channels import push
from channels.models import EmailSender, PushApp, TelegramBot, Webhook
from channels.tasks import poll_telegram
from channels.telegram import TelegramError
from core.models import Building, TeamMember
from delivery import pipeline
from delivery.tests import helpers as h
from people.models import List

pytestmark = pytest.mark.django_db


def _client(role="owner"):
    TeamMember.objects.get_or_create(email="owner@maplegrove.example", defaults={"role": "owner", "name": "Andrew"})
    user, _ = TeamMember.objects.get_or_create(email=f"{role}@maplegrove.example", defaults={"role": role, "name": role.title()})
    client = Client()
    client.force_login(user)
    return client


GMAIL_FORM = {"purpose": "newsletter", "kind": "gmail", "from_name": "Maple Grove Youth",
              "from_email": "youth@maplegrove.example", "password": "abcd efgh ijkl mnop"}


def test_a_viewer_cannot_open_channels(org):
    assert _client("viewer").get("/channels/").status_code == 403
    assert _client("editor").get("/channels/").status_code == 403
    assert _client("viewer").post("/channels/email/add/", GMAIL_FORM).status_code == 403
    assert EmailSender.objects.count() == 0


def test_owner_sees_four_sections(org):
    html = _client().get("/channels/").content.decode()
    for heading in ("Email", "Telegram", "Push notifications", "Webhooks", "Add a newsletter sender", "Add a bot",
                    "Connect an app", "Add a webhook"):
        assert heading in html


def test_test_and_save_saves_only_when_the_server_lets_us_in(org):
    client = _client()
    refused = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value.login.side_effect = refused
        response = client.post("/channels/email/add/", GMAIL_FORM)
    assert response.status_code == 200 and EmailSender.objects.count() == 0
    html = response.content.decode()
    assert "Username and Password not accepted" in html and "youth@maplegrove.example" in html
    assert "abcd efgh" not in html  # the password is never echoed back

    with mock.patch("smtplib.SMTP") as smtp_class:
        response = client.post("/channels/email/add/", GMAIL_FORM)
    assert response.status_code == 302
    smtp_class.assert_called_once_with("smtp.gmail.com", 587, timeout=mock.ANY)
    smtp_class.return_value.login.assert_called_once_with("youth@maplegrove.example", "abcdefghijklmnop")
    sender = EmailSender.objects.get()
    assert sender.purpose == "newsletter" and sender.is_default and sender.state == "ok"
    assert sender.daily_limit is None and sender.per_second == 5
    html = client.get("/channels/").content.decode()
    assert "Maple Grove Youth" in html and "abcd efgh ijkl mnop" not in html and "abcdefghijklmnop" not in html


def test_there_is_exactly_one_app_sender_and_it_stays_while_buildings_are_connected(org):
    client = _client()
    form = dict(GMAIL_FORM, purpose="app", from_email="hello@maplegrove.example", from_name="Maple Grove")
    with mock.patch("smtplib.SMTP"):
        client.post("/channels/email/add/", form)
        client.post("/channels/email/add/", dict(form, from_email="office@maplegrove.example"))
    sender = EmailSender.objects.get(purpose="app")
    assert sender.from_email == "office@maplegrove.example"  # replaced, not duplicated
    h.make_building()
    client.post(f"/channels/email/{sender.public_id}/delete/")
    assert EmailSender.objects.filter(purpose="app").exists()
    Building.objects.all().delete()
    client.post(f"/channels/email/{sender.public_id}/delete/")
    assert not EmailSender.objects.filter(purpose="app").exists()


def test_send_a_test_emails_the_signed_in_team_member_and_default_can_move(org, smtp):
    first = h.make_sender()
    second = h.make_sender(from_email="events@maplegrove.example", is_default=False)
    client = _client()
    assert client.post(f"/channels/email/{first.public_id}/test/").status_code == 302
    assert smtp.sent[0][0] == "owner@maplegrove.example"
    client.post(f"/channels/email/{second.public_id}/default/")
    assert EmailSender.default_newsletter() == second
    assert EmailSender.objects.filter(is_default=True).count() == 1


def test_adding_a_bot_checks_the_token_and_reads_the_username(org, settings):
    client = _client()
    with mock.patch("channels.telegram.TelegramClient") as tg:
        tg.return_value.get_me.side_effect = TelegramError(401, "Unauthorized")
        response = client.post("/channels/telegram/add/", {"token": "bad"})
    assert response.status_code == 200 and "Telegram does not know this token" in response.content.decode()
    assert TelegramBot.objects.count() == 0

    settings.TELEGRAM_MODE = "webhook"
    with mock.patch("channels.telegram.TelegramClient") as tg:
        tg.return_value.get_me.return_value = {"id": 1, "first_name": "Maple Grove", "username": "MapleGroveBot"}
        response = client.post("/channels/telegram/add/", {"token": "123:abc"})
    assert response.status_code == 302
    bot = TelegramBot.objects.get()
    assert bot.username == "MapleGroveBot" and bot.name == "Maple Grove" and bot.is_default and bot.token == "123:abc"
    tg.return_value.set_webhook.assert_called_once_with(
        f"https://post.maplegrove.example/tg/{bot.public_id}/{bot.webhook_secret}")
    html = client.get("/channels/").content.decode()
    assert "@MapleGroveBot" in html and "123:abc" not in html


def test_telegram_webhook_checks_the_secret_and_hands_the_update_over(org):
    bot = h.make_bot()
    update = {"update_id": 77, "message": {"text": "/help", "chat": {"id": 5}}}
    client = Client()
    with mock.patch("commands.handler.process_update") as handler:
        wrong = client.post(f"/tg/{bot.public_id}/wrong-secret", json.dumps(update), content_type="application/json")
        assert wrong.status_code == 404 and not handler.called
        right = client.post(f"/tg/{bot.public_id}/{bot.webhook_secret}", json.dumps(update), content_type="application/json")
    assert right.status_code == 200
    handler.assert_called_once()
    assert handler.call_args[0][0] == bot and handler.call_args[0][1] == update


def test_polling_hands_each_update_over_and_moves_the_offset(org, settings):
    settings.TELEGRAM_MODE = "polling"
    bot = h.make_bot(last_update_id=10)
    updates = [{"update_id": 11, "message": {"text": "/start"}}, {"update_id": 12, "message": {"text": "/help"}}]
    with mock.patch("channels.telegram.TelegramClient") as tg, mock.patch("commands.handler.process_update") as handler:
        tg.return_value.get_updates.return_value = updates
        assert poll_telegram() == 2
    tg.return_value.get_updates.assert_called_once_with(offset=11)
    assert handler.call_count == 2
    bot.refresh_from_db()
    assert bot.last_update_id == 12
    settings.TELEGRAM_MODE = "webhook"
    with mock.patch("channels.telegram.TelegramClient") as tg:
        assert poll_telegram() == 0 and not tg.called


DISCOVERY = {"name": "Pavilion", "kind": "pavilion", "notification_endpoint": "/api/notifications",
             "audiences": [{"audience": "offers-and-news", "label": "Offers and news"},
                           {"audience": "announcements", "label": "Announcements"}]}


def test_connecting_an_app_discovers_its_audiences_and_makes_a_list_for_each(org):
    client = _client()
    with mock.patch("httpx.get", return_value=httpx.Response(200, json=DISCOVERY)) as get:
        response = client.post("/channels/push/connect/", {"address": "pavilion.maplegrove.example"})
    assert response.status_code == 302
    assert get.call_args[0][0] == "https://pavilion.maplegrove.example/api/post-office"
    building = Building.objects.get()
    assert building.name == "Pavilion" and building.kind == "pavilion"
    apps = list(PushApp.objects.order_by("id"))
    assert [a.audience for a in apps] == ["offers-and-news", "announcements"]
    assert all(a.endpoint == "https://pavilion.maplegrove.example/api/notifications" for a in apps)
    lists = list(List.objects.order_by("id"))
    assert [(lst.kind, lst.building, lst.name) for lst in lists] == [
        ("campaign", building, {"en": "Offers and news"}), ("campaign", building, {"en": "Announcements"})]
    assert [a.list for a in apps] == lists
    # Connecting again changes nothing.
    with mock.patch("httpx.get", return_value=httpx.Response(200, json=DISCOVERY)):
        client.post("/channels/push/connect/", {"address": "https://pavilion.maplegrove.example/"})
    assert PushApp.objects.count() == 2 and List.objects.count() == 2 and Building.objects.count() == 1
    html = client.get("/channels/").content.decode()
    assert "Offers and news" in html and apps[0].secret not in html
    shown = client.post(f"/channels/push/{apps[0].public_id}/secret/").content.decode()
    assert apps[0].secret in shown


def test_an_address_that_is_not_an_app_says_so(org):
    with mock.patch("httpx.get", return_value=httpx.Response(404)):
        response = _client().post("/channels/push/connect/", {"address": "example.org"})
    assert response.status_code == 200 and "did not answer like an app" in response.content.decode()
    assert PushApp.objects.count() == 0


def test_webhook_gets_a_signed_copy_of_the_campaign_at_send_time(org, smtp):
    client = _client()
    client.post("/channels/webhooks/add/", {"name": "Lobby display", "url": "https://lobby.maplegrove.example/hook"})
    off = Webhook.objects.create(name="Chat room", url="https://chat.maplegrove.example/hook")
    client.post(f"/channels/webhooks/{off.public_id}/toggle/", {})
    hook = Webhook.objects.get(name="Lobby display")
    off.refresh_from_db()
    assert hook.enabled and not off.enabled
    newsletter = h.make_list()
    h.subscribe(h.make_person(), newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender(), languages=("en", "uk"))
    with mock.patch("httpx.post", return_value=httpx.Response(200)) as post:
        pipeline.send_campaign(campaign)
        pipeline.send_campaign(campaign)  # a repeated fan-out does not post again
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://lobby.maplegrove.example/hook"
    body = kwargs["content"]
    assert kwargs["headers"]["X-PostOffice-Signature"] == "sha256=" + hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
    payload = json.loads(body)
    assert payload["name"] == "Winter coat drive" and payload["lists"] == [{"slug": "newsletter", "name": {"en": "Newsletter"}}]
    assert payload["content"]["uk"] == {"subject": "Coat drive (uk)", "notification": "*Coat drive* this Saturday (uk)",
                                        "button": {"label": "Details", "url": "https://maplegrove.example/coats"}}
    hook.refresh_from_db()
    assert hook.last_status.startswith("ok 200")


def test_push_signature_matches_the_spec():
    body = b'{"audience":"offers-and-news"}'
    assert push.sign("s3cret", body) == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


def test_the_card_says_how_many_wait_for_tomorrow(org, smtp):
    newsletter = h.make_list()
    for n in (1, 2, 3):
        h.subscribe(h.make_person(f"person{n}@example.com", f"Person{n}"), newsletter)
    sender = h.make_sender(daily_limit=2)
    h.make_sender("app", from_email="hello@maplegrove.example")
    pipeline.send_campaign(h.make_campaign([newsletter], sender=sender))
    building, _key = h.make_building()
    h.make_push_app(building, newsletter)
    h.make_bot()
    Webhook.objects.create(name="Lobby display", url="https://lobby.maplegrove.example/hook")
    html = _client().get("/channels/").content.decode()
    assert "2 sent today, 1 waiting until tomorrow" in html
    assert "2 emails, the rest wait until tomorrow" in html
    assert "Lobby display" in html and "@MapleGroveBot" in html and "pavilion.maplegrove.example/api/notifications" in html
    assert "shared-secret" not in html and ">secret<" not in html
