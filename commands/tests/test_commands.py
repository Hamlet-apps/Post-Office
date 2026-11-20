"""Telegram commands (spec section 12). Nothing here touches the network:
httpx and channels.telegram.TelegramClient are always patched."""
import json
import logging
from datetime import timedelta
from unittest import mock

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from channels.models import TelegramBot, TelegramLink
from channels.telegram import TelegramError
from commands import forward
from commands.handler import RATE_LIMIT, late_reply, process_update, sync_bot_menu
from commands.models import BotText, Command, CommandLog
from core.fields import decrypt
from core.models import Language, TeamMember
from core.views import save_languages
from people import services, tokens
from people.models import List, Person, Subscription

pytestmark = pytest.mark.django_db

CHAT = 48213377
SECRET = "Bearer mg_feedback_key_do_not_leak"


class FakeTelegram:
    """Stands in for TelegramClient: records what would have been sent."""

    sent = []  # (chat_id, html, buttons)
    menus = []  # (language_code, commands)
    callbacks = []
    fail_with = None

    def __init__(self, bot=None, token=None, timeout=15):
        self.bot = bot

    def send(self, chat_id, html, button=None, image_url=None, buttons=None):
        if FakeTelegram.fail_with:
            raise FakeTelegram.fail_with
        FakeTelegram.sent.append((chat_id, html, buttons))
        return {"message_id": len(FakeTelegram.sent)}

    def set_my_commands(self, commands, language_code=None):
        if FakeTelegram.fail_with:
            raise FakeTelegram.fail_with
        FakeTelegram.menus.append((language_code, commands))
        return True

    def answer_callback(self, callback_query_id, text=None):
        FakeTelegram.callbacks.append((callback_query_id, text))
        return True


@pytest.fixture(autouse=True)
def telegram_client(settings):
    cache.clear()
    settings.ALLOW_PRIVATE_TARGETS = True  # skips the DNS lookup; the safety tests switch it off
    settings.PUBLIC_URL = "https://post.example"
    FakeTelegram.sent, FakeTelegram.menus, FakeTelegram.callbacks, FakeTelegram.fail_with = [], [], [], None
    with mock.patch("channels.telegram.TelegramClient", FakeTelegram):
        yield FakeTelegram


@pytest.fixture
def bot():
    save_languages(["en", "uk", "fr"], "en")
    return TelegramBot.objects.create(name="Maple Grove", username="MapleGroveBot", token="123:test-token")


@pytest.fixture
def taras(bot):
    person = Person.objects.create(email="taras@example.com", first_name="Taras", language=Language.objects.get(code="uk"))
    TelegramLink.objects.create(person=person, bot=bot, chat_id=CHAT)
    return person


def message(text, chat_id=CHAT):
    return {"update_id": 1, "message": {"message_id": 5, "chat": {"id": chat_id, "type": "private"},
                                        "from": {"id": chat_id, "first_name": "Taras", "username": "taras_m"}, "text": text}}


def make_command(bot, **kwargs):
    defaults = {"name": "shifts", "method": "POST", "url": "https://hooks.maplegrove.example/shifts",
                "body": forward.DEFAULT_BODY, "reply_path": "reply",
                "descriptions": {"en": "My upcoming shifts", "uk": "Мої найближчі зміни"}}
    defaults.update(kwargs)
    return Command.objects.create(bot=bot, **defaults)


def answer(body=None, status=200, text=None):
    """An httpx response for the patched httpx.request."""
    if text is None:
        text = json.dumps(body if body is not None else {"reply": "You are on *Saturday* at 9."})
    return httpx.Response(status, text=text, request=httpx.Request("POST", "https://hooks.maplegrove.example/"))


def patch_http(**kwargs):
    return mock.patch("commands.forward.httpx.request", **kwargs)


# --------------------------------------------------------------------------- built-in commands

def test_help_lists_commands_in_the_persons_language(bot, taras, telegram_client):
    make_command(bot)
    make_command(bot, name="hidden", enabled=False, descriptions={"en": "Switched off"})
    process_update(bot, message("/help"))
    chat_id, html, _buttons = telegram_client.sent[-1]
    assert chat_id == CHAT
    assert "/shifts - Мої найближчі зміни" in html  # Ukrainian, the person's language
    assert "My upcoming shifts" not in html and "/hidden" not in html
    assert "/start" in html and "/stop" in html and "/language" in html
    log = CommandLog.objects.get()
    assert (log.command, log.target, log.result, log.person) == ("help", "built_in", "answered", taras)


def test_help_for_an_unknown_chat_uses_the_default_language(bot, telegram_client):
    make_command(bot)
    process_update(bot, message("/help@MapleGroveBot"))
    assert "/shifts - My upcoming shifts" in telegram_client.sent[-1][1]


def test_text_that_is_not_a_command_is_ignored(bot, taras, telegram_client):
    process_update(bot, message("hello there"))
    process_update(bot, message("/help@SomeOtherBot"))
    assert telegram_client.sent == [] and not CommandLog.objects.exists()


def test_start_with_a_token_links_the_chat(bot, telegram_client):
    person = Person.objects.create(email="olena@example.com", first_name="Olena")
    app_list = List.objects.create(name={"en": "Volunteers"}, slug="volunteers", kind="app")
    news = List.objects.create(name={"en": "Newsletter"}, slug="newsletter", kind="campaign")
    other_news = List.objects.create(name={"en": "Events"}, slug="events", kind="campaign")
    services.set_subscription(person, app_list, "email", True)
    consent = services.record_consent(person, [news], "signup_form", "I agree to receive the newsletter")
    services.set_subscription(person, news, "email", True, consent=consent)
    token = tokens.connect_token(person)

    process_update(bot, message(f"/start {token}", chat_id=777))

    link = TelegramLink.objects.get(bot=bot, chat_id=777)
    assert link.person == person and link.blocked_at is None and link.username == "taras_m"
    assert "Olena" in telegram_client.sent[-1][1]
    on = set(Subscription.objects.filter(person=person, channel="telegram", state="on").values_list("list__slug", flat=True))
    assert on == {"volunteers", "newsletter"}  # the app list, and the newsletter they consented to by email
    assert Subscription.objects.get(person=person, list=news, channel="telegram").consent == consent
    assert not Subscription.objects.filter(person=person, list=other_news).exists()  # never without consent
    assert CommandLog.objects.get().person == person
    # The token is single-use.
    process_update(bot, message(f"/start {token}", chat_id=888))
    assert not TelegramLink.objects.filter(chat_id=888).exists()


def test_start_without_a_token_offers_the_signup_form(bot, telegram_client):
    process_update(bot, message("/start"))
    assert "https://post.example/join" in telegram_client.sent[-1][1]


def test_start_again_clears_a_block_and_shows_preferences(bot, taras, telegram_client):
    TelegramLink.objects.filter(chat_id=CHAT).update(blocked_at=timezone.now())
    process_update(bot, message("/start"))
    assert TelegramLink.objects.get(chat_id=CHAT).blocked_at is None
    assert tokens.preferences_url(taras) in telegram_client.sent[-1][1]


def test_stop_turns_off_telegram_newsletters_only(bot, taras, telegram_client):
    news = List.objects.create(name={"en": "Newsletter"}, slug="newsletter", kind="campaign")
    app_list = List.objects.create(name={"en": "Volunteers"}, slug="volunteers", kind="app")
    consent = services.record_consent(taras, [news], "signup_form", "I agree")
    services.set_subscription(taras, news, "email", True, consent=consent)
    services.set_subscription(taras, news, "telegram", True, consent=consent)
    services.set_subscription(taras, app_list, "telegram", True)

    process_update(bot, message("/stop"))

    def state(lst, channel):
        return Subscription.objects.get(person=taras, list=lst, channel=channel).state

    assert state(news, "telegram") == "off"
    assert state(news, "email") == "on"  # email is untouched
    assert state(app_list, "telegram") == "on"  # service messages still arrive
    # /start after /stop restores nothing by itself: it shows the preferences link.
    process_update(bot, message("/start"))
    assert state(news, "telegram") == "off"
    assert tokens.preferences_url(taras) in telegram_client.sent[-1][1]


def test_language_shows_buttons_and_saves_the_choice(bot, taras, telegram_client):
    process_update(bot, message("/language"))
    buttons = [b for row in telegram_client.sent[-1][2] for b in row]
    assert [b["callback_data"] for b in buttons] == ["lang:en", "lang:uk", "lang:fr"]
    process_update(bot, {"update_id": 2, "callback_query": {
        "id": "cb1", "data": "lang:fr", "from": {"id": CHAT}, "message": {"message_id": 6, "chat": {"id": CHAT}}}})
    taras.refresh_from_db()
    assert taras.language.code == "fr"
    assert telegram_client.callbacks[-1][0] == "cb1" and "Français" in telegram_client.sent[-1][1]


# --------------------------------------------------------------------------- passing commands on

def test_an_answer_is_sent_back_with_formatting(bot, taras, telegram_client):
    make_command(bot)
    with patch_http(return_value=answer()) as request:
        process_update(bot, message("/shifts saturday"))
    assert telegram_client.sent[-1][1] == "You are on <b>Saturday</b> at 9."
    log = CommandLog.objects.get()
    assert (log.command, log.target, log.result, log.text) == ("shifts", "http", "answered", "/shifts saturday")
    # The standard payload, with the address for a later answer.
    sent = json.loads(request.call_args.kwargs["content"])
    assert sent == {
        "command": "shifts", "text": "/shifts saturday", "arguments": ["saturday"], "bot": "MapleGroveBot",
        "person": {"id": taras.public_id, "first_name": "Taras", "language": "uk", "telegram_id": CHAT},
        "reply_url": f"https://post.example/api/v1/commands/{log.public_id}/reply?token={log.reply_token}",
    }
    assert request.call_args.kwargs["timeout"] == 10


def test_a_target_that_does_not_answer_in_10_seconds_gets_the_fallback_reply(bot, taras, telegram_client):
    make_command(bot)
    BotText.objects.create(bot=bot, language="uk", no_answer="Зараз не працює. Спробуйте пізніше.")
    with patch_http(side_effect=httpx.ReadTimeout("timed out")):
        process_update(bot, message("/shifts"))
    assert telegram_client.sent[-1][1] == "Зараз не працює. Спробуйте пізніше."
    log = CommandLog.objects.get()
    assert log.result == "no_answer" and log.target == "http"


def test_a_connection_error_or_a_bad_status_is_logged_as_error(bot, taras, telegram_client):
    make_command(bot)
    with patch_http(side_effect=httpx.ConnectError("refused")):
        process_update(bot, message("/shifts"))
    with patch_http(return_value=answer(status=500)):
        process_update(bot, message("/shifts"))
    assert list(CommandLog.objects.values_list("result", flat=True)) == ["error", "error"]
    # No Ukrainian text saved for this bot: the built-in wording is used.
    assert all("try again later" in html for _c, html, _b in telegram_client.sent)


def test_an_unknown_command_gets_the_unknown_text_and_a_log_row(bot, taras, telegram_client):
    make_command(bot, name="off", enabled=False)
    BotText.objects.create(bot=bot, language="uk", unknown_command="Я не знаю такої команди.")
    with patch_http() as request:
        process_update(bot, message("/menu"))
        process_update(bot, message("/off"))
    assert not request.called
    assert [html for _c, html, _b in telegram_client.sent] == ["Я не знаю такої команди."] * 2
    assert list(CommandLog.objects.values_list("command", "result", "target")) == [("off", "unknown", ""), ("menu", "unknown", "")]


def test_an_unknown_chat_gets_the_unknown_text_in_the_default_language(bot, telegram_client):
    BotText.objects.create(bot=bot, language="en", unknown_command="No such command here.")
    process_update(bot, message("/menu"))
    assert telegram_client.sent[-1][1] == "No such command here."
    assert CommandLog.objects.get().person is None


def test_a_connected_only_command_asks_an_unknown_chat_to_press_start(bot, telegram_client):
    make_command(bot)  # connected_only is on by default
    with patch_http() as request:
        process_update(bot, message("/shifts"))
    assert not request.called
    assert "/start" in telegram_client.sent[-1][1]
    assert CommandLog.objects.get().command == "shifts"
    # With the switch off, the same chat is passed through.
    Command.objects.update(connected_only=False)
    with patch_http(return_value=answer({"reply": "Open 9 to 5"})) as request:
        process_update(bot, message("/shifts"))
    assert request.called and telegram_client.sent[-1][1] == "Open 9 to 5"


def test_no_reply_and_empty_answers_send_nothing(bot, taras, telegram_client):
    make_command(bot, reply="none")
    with patch_http(return_value=answer({"reply": "ignored"})):
        process_update(bot, message("/shifts"))
    Command.objects.update(reply="answer")
    with patch_http(return_value=answer(text="")):
        process_update(bot, message("/shifts"))
    with patch_http(return_value=answer({"other": "field"})):
        process_update(bot, message("/shifts"))
    assert telegram_client.sent == []
    assert set(CommandLog.objects.values_list("result", flat=True)) == {"answered"}


def test_reply_field_paths(bot):
    assert forward.extract_reply('{"data": {"message": "Hi"}}', "data.message") == ("Hi", "")
    assert forward.extract_reply('{"items": [{"t": 3}]}', "items.0.t") == ("3", "")
    assert forward.extract_reply("plain words", "") == ("plain words", "")
    assert forward.extract_reply("plain words", "reply")[1]  # not JSON: a problem, not a reply


def test_placeholders_are_url_encoded_in_the_address_and_json_escaped_in_the_body(bot, taras, telegram_client):
    make_command(bot, name="feedback", method="POST",
                 url="https://hooks.maplegrove.example/f?who={{person.first_name}}&q={{arguments}}&c={{command}}",
                 body='{"from": "{{ person.first_name }}", "message": "{{arguments}}", "id": {{person.telegram_id}}}')
    text = '/feedback more "parking" & café\\please'
    with patch_http(return_value=answer({"reply": "ok"})) as request:
        process_update(bot, message(text))
    method, url = request.call_args.args
    assert method == "POST"
    assert url == ("https://hooks.maplegrove.example/f?who=Taras&q=more%20%22parking%22%20%26%20caf%C3%A9%5Cplease&c=feedback")
    body = json.loads(request.call_args.kwargs["content"])  # still valid JSON
    assert body == {"from": "Taras", "message": 'more "parking" & café\\please', "id": CHAT}


def test_get_and_delete_send_no_body(bot, taras):
    command = make_command(bot, method="GET", reply_path="",
                           url="https://hooks.maplegrove.example/hours?lang={{person.language}}")
    with patch_http(return_value=answer(text="Open 9 to 5")) as request:
        result = forward.send(command, forward.build_values("shifts", "/shifts", person=taras, bot=bot, chat_id=CHAT))
    assert request.call_args.args == ("GET", "https://hooks.maplegrove.example/hours?lang=uk")
    assert request.call_args.kwargs["content"] is None
    assert result.ok and result.reply_text == "Open 9 to 5"  # no reply field: the whole answer


def test_header_values_are_stored_encrypted_and_never_logged(bot, taras, telegram_client, caplog):
    owner = TeamMember.objects.create_user("owner@example.com", "long password", role="owner")
    client = Client()
    client.force_login(owner)
    response = client.post("/commands/save/", {
        "bot": bot.public_id, "name": "feedback", "method": "POST", "url": "https://hooks.maplegrove.example/feedback",
        "header_name": ["Content-Type", "Authorization"], "header_value": ["application/json", SECRET], "header_orig": ["", ""],
        "body": '{"m": "{{arguments}}"}', "reply_path": "reply", "reply": "answer", "connected_only": "on", "enabled": "on",
        "desc_en": "Tell us something",
    })
    assert response.status_code == 302
    command = Command.objects.get(name="feedback")
    stored = json.dumps(command.headers)
    assert SECRET not in stored and "application/json" not in stored
    assert all(h["value"].startswith("fernet:") for h in command.headers)
    assert decrypt(command.headers[1]["value"]) == SECRET

    # Shown masked after saving: neither the page nor the preview repeats the value.
    page = client.get(f"/commands/?bot={bot.public_id}&cmd={command.public_id}")
    assert page.status_code == 200 and SECRET not in page.content.decode()
    assert "Authorization: ••••••••" in page.content.decode()

    # Saving again without retyping keeps the stored value.
    client.post("/commands/save/", {
        "bot": bot.public_id, "id": command.public_id, "name": "feedback", "method": "POST",
        "url": "https://hooks.maplegrove.example/feedback", "header_name": ["Content-Type", "Authorization"],
        "header_value": ["", ""], "header_orig": ["0", "1"], "body": '{"m": "{{arguments}}"}', "enabled": "on",
    })
    command.refresh_from_db()
    assert decrypt(command.headers[1]["value"]) == SECRET

    # The request carries the real value; nothing written to the logs does.
    with caplog.at_level(logging.DEBUG):
        with patch_http(return_value=answer({"reply": "Thanks"})) as request:
            process_update(bot, message("/feedback hello"))
        with patch_http(side_effect=httpx.ConnectError(f"failed with {SECRET}")):
            process_update(bot, message("/feedback hello"))
    assert request.call_args.kwargs["headers"] == {"Content-Type": "application/json", "Authorization": SECRET}
    assert SECRET not in caplog.text
    for log in CommandLog.objects.all():
        assert SECRET not in json.dumps([log.text, log.command, log.target, log.result])
    activity = client.get(f"/commands/?bot={bot.public_id}").content.decode()
    assert SECRET not in activity


def test_a_loopback_address_is_refused_unless_private_targets_are_allowed(bot, taras, telegram_client, settings):
    settings.ALLOW_PRIVATE_TARGETS = False
    for url in ("http://127.0.0.1:8000/hook", "http://localhost/hook", "http://[::1]/hook", "http://169.254.169.254/latest"):
        with pytest.raises(forward.TargetRefused):
            forward.check_address(url)
    with pytest.raises(forward.TargetRefused):
        forward.check_address("ftp://example.org/x")
    # A public name that resolves to a loopback address is refused too: every address is checked.
    resolved = [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]
    with mock.patch("commands.forward.socket.getaddrinfo", return_value=resolved):
        with pytest.raises(forward.TargetRefused):
            forward.check_address("https://sneaky.example/hook")
    with mock.patch("commands.forward.socket.getaddrinfo", return_value=resolved[:1]):
        forward.check_address("https://fine.example/hook")

    make_command(bot, url="http://127.0.0.1:8000/hook")
    with patch_http(return_value=answer()) as request:
        process_update(bot, message("/shifts"))
        assert not request.called
        assert CommandLog.objects.get().result == "error"
        assert "try again later" in telegram_client.sent[-1][1]

        settings.ALLOW_PRIVATE_TARGETS = True
        forward.check_address("http://127.0.0.1:8000/hook")
        process_update(bot, message("/shifts"))
        assert request.called


def test_late_reply_works_once(bot, taras, telegram_client):
    make_command(bot)
    with patch_http(return_value=answer(text="")):
        process_update(bot, message("/shifts"))
    log = CommandLog.objects.get()
    assert late_reply(log, "Your shift moved to *Sunday*.") is True
    assert telegram_client.sent == [(CHAT, "Your shift moved to <b>Sunday</b>.", None)]
    log.refresh_from_db()
    assert log.late_replied_at is not None
    assert late_reply(log, "Again") is False
    assert late_reply(CommandLog.objects.get(), "Again") is False
    assert len(telegram_client.sent) == 1


def test_late_reply_is_refused_after_24_hours(bot, taras, telegram_client):
    log = CommandLog.objects.create(bot=bot, person=taras, chat_id=CHAT, text="/shifts", command="shifts", target="http")
    CommandLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - timedelta(hours=25))
    log.refresh_from_db()
    assert late_reply(log, "Too late") is False
    assert telegram_client.sent == [] and CommandLog.objects.get().late_replied_at is None


def test_each_chat_is_limited_to_20_commands_per_minute(bot, taras, telegram_client):
    for _ in range(RATE_LIMIT + 5):
        process_update(bot, message("/help"))
    assert CommandLog.objects.count() == RATE_LIMIT == 20
    assert len(telegram_client.sent) == RATE_LIMIT + 1  # twenty answers and one "please wait"
    # Another chat is not affected.
    process_update(bot, message("/help", chat_id=999))
    assert CommandLog.objects.filter(chat_id=999).count() == 1


def test_a_blocked_bot_marks_the_link(bot, taras, telegram_client):
    telegram_client.fail_with = TelegramError(403, "Forbidden: bot was blocked by the user")
    process_update(bot, message("/help"))
    assert TelegramLink.objects.get(chat_id=CHAT).blocked_at is not None
    assert CommandLog.objects.get().result == "error"


# --------------------------------------------------------------------------- the bot's menu

def test_sync_bot_menu_sets_commands_once_per_language(bot, telegram_client):
    make_command(bot)
    make_command(bot, name="hidden", enabled=False)
    sync_bot_menu(bot)
    assert [code for code, _commands in telegram_client.menus] == [None, "uk", "fr"]  # the default has no code
    by_code = dict(telegram_client.menus)
    assert [c["command"] for c in by_code["uk"]] == ["start", "stop", "language", "help", "shifts"]
    assert {"command": "shifts", "description": "Мої найближчі зміни"} in by_code["uk"]
    assert {"command": "shifts", "description": "My upcoming shifts"} in by_code[None]
    assert {"command": "shifts", "description": "My upcoming shifts"} in by_code["fr"]  # falls back to the default


# --------------------------------------------------------------------------- the Commands screen

@pytest.fixture
def owner_client(bot):
    owner = TeamMember.objects.create_user("owner@example.com", "long password", role="owner", name="Olena Owner")
    client = Client()
    client.force_login(owner)
    return client


def test_a_viewer_and_an_editor_cannot_open_commands(bot):
    TeamMember.objects.create_user("owner@example.com", "long password", role="owner")
    command = make_command(bot)
    for role in ("viewer", "editor"):
        member = TeamMember.objects.create_user(f"{role}@example.com", "long password", role=role)
        client = Client()
        client.force_login(member)
        assert client.get("/commands/").status_code == 403
        assert client.post("/commands/save/", {"bot": bot.public_id, "name": "x", "url": "https://x.example"}).status_code == 403
        assert client.post(f"/commands/{command.public_id}/delete/").status_code == 403
        assert client.post("/commands/test/", {"bot": bot.public_id}).status_code == 403
    assert Command.objects.count() == 1
    assert Client().get("/commands/").status_code == 302  # signed out: to the sign-in page


def test_the_screen_without_a_bot_points_to_channels():
    TeamMember.objects.create_user("owner@example.com", "long password", role="owner")
    client = Client()
    client.force_login(TeamMember.objects.get())
    page = client.get("/commands/")
    assert page.status_code == 200 and "/channels/" in page.content.decode()


def test_creating_editing_and_deleting_a_command_updates_the_menu(bot, owner_client, telegram_client):
    page = owner_client.get(f"/commands/?bot={bot.public_id}&new=1")
    assert page.status_code == 200 and "reply_url" in page.content.decode()  # the standard body is suggested
    data = {"bot": bot.public_id, "name": "/Hours", "method": "GET", "url": "https://hooks.maplegrove.example/hours",
            "desc_en": "Opening hours", "desc_uk": "Години роботи", "reply": "answer", "enabled": "on", "body": "ignored for GET"}
    response = owner_client.post("/commands/save/", data)
    command = Command.objects.get()
    assert response.status_code == 302 and command.public_id in response["Location"]
    assert (command.name, command.method, command.body, command.connected_only, command.enabled) == ("hours", "GET", "", False, True)
    assert command.descriptions == {"en": "Opening hours", "uk": "Години роботи"}
    assert len(telegram_client.menus) == 3 and "hours" in [c["command"] for c in telegram_client.menus[0][1]]

    page = owner_client.get(f"/commands/?bot={bot.public_id}").content.decode()
    assert "/hours" in page and "Opening hours" in page and "Always on" in page and "Switchboard" not in page

    owner_client.post(f"/commands/{command.public_id}/toggle/", {})
    assert Command.objects.get().enabled is False
    assert "hours" not in [c["command"] for c in telegram_client.menus[-1][1]]

    owner_client.post(f"/commands/{command.public_id}/delete/")
    assert not Command.objects.exists()


def test_command_names_are_checked(bot, owner_client):
    make_command(bot)
    base = {"bot": bot.public_id, "method": "POST", "url": "https://hooks.maplegrove.example/x"}
    for name in ("help", "start", "shifts", "Has Space", "x" * 33, "naïve", ""):
        response = owner_client.post("/commands/save/", {**base, "name": name})
        assert response.status_code == 422, name
    assert owner_client.post("/commands/save/", {**base, "name": "ok_1", "url": "hooks.example"}).status_code == 422
    assert Command.objects.count() == 1
    assert owner_client.post("/commands/save/", {**base, "name": "a" * 32}).status_code == 302


def test_a_telegram_error_does_not_fail_the_save(bot, owner_client, telegram_client):
    telegram_client.fail_with = TelegramError(401, "Unauthorized")
    response = owner_client.post("/commands/save/", {"bot": bot.public_id, "name": "hours", "method": "GET",
                                                     "url": "https://hooks.maplegrove.example/hours"}, follow=True)
    assert Command.objects.filter(name="hours").exists()
    assert "could not be updated" in response.content.decode()


def test_the_bots_own_texts_are_saved_per_language(bot, owner_client):
    owner_client.post("/commands/texts/", {"bot": bot.public_id, "unknown_en": "No idea.", "no_answer_uk": "Не працює."})
    assert BotText.objects.get(bot=bot, language="en").unknown_command == "No idea."
    assert BotText.objects.get(bot=bot, language="uk").no_answer == "Не працює."
    assert BotText.objects.get(bot=bot, language="fr").unknown_command == ""


def test_the_live_preview_shows_the_exact_request(bot, owner_client):
    response = owner_client.post("/commands/preview/", {
        "bot": bot.public_id, "name": "feedback", "method": "POST",
        "url": "https://hooks.maplegrove.example/feedback?from={{person.first_name}}&q={{arguments}}",
        "header_name": ["Authorization"], "header_value": ["Bearer typed-just-now"], "header_orig": [""],
        "body": '{"message": "{{arguments}}", "lang": "{{person.language}}"}', "reply_path": "data.message",
    })
    text = response.content.decode()
    assert "POST https://hooks.maplegrove.example/feedback?from=Taras&amp;q=more%20parking%20please" in text
    assert "&quot;message&quot;: &quot;more parking please&quot;, &quot;lang&quot;: &quot;uk&quot;" in text
    assert "Authorization: Bearer typed-just-now" in text
    assert not Command.objects.exists()  # a preview saves nothing


def test_send_a_test_fires_the_request_as_the_signed_in_member(bot, owner_client, telegram_client):
    data = {"bot": bot.public_id, "name": "feedback", "method": "POST", "url": "https://hooks.maplegrove.example/feedback",
            "body": '{"from": "{{person.first_name}}", "text": "{{text}}"}', "reply_path": "reply", "reply": "answer"}
    with patch_http(return_value=answer({"reply": "Thanks, *Olena*."})) as request:
        response = owner_client.post("/commands/test/", data)
    assert json.loads(request.call_args.kwargs["content"]) == {"from": "Olena", "text": "/feedback test"}
    assert "Thanks, <b>Olena</b>." in response.content.decode()
    with patch_http(side_effect=httpx.ReadTimeout("slow")):
        response = owner_client.post("/commands/test/", data)
    assert "No answer" in response.content.decode()
    assert telegram_client.sent == [] and not CommandLog.objects.exists() and not Command.objects.exists()
