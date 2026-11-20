"""Handling Telegram updates (spec section 12).

Three functions other areas call:

    process_update(bot, update)   every update from polling and from the webhook route
    late_reply(log, text)         a target answers later, once, within 24 hours
    sync_bot_menu(bot)            after any change to commands: setMyCommands per language

Post Office is a plain proxy here: built-in commands are answered directly,
everything else is passed to the command's web address and the answer is sent back.
"""
import logging
import time
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from channels import telegram
from channels.models import TelegramLink
from content.render import telegram_html
from core.models import Language

from . import builtin, forward
from .models import Command, CommandLog

logger = logging.getLogger(__name__)

RATE_LIMIT = 20  # commands per chat per minute
RATE_WINDOW_SECONDS = 60
LATE_REPLY_WINDOW = timedelta(hours=24)


# --------------------------------------------------------------------------- public

def process_update(bot, update: dict) -> None:
    """Handle one Telegram update for this bot. Never raises for a bad update."""
    try:
        if update.get("callback_query"):
            _handle_callback(bot, update["callback_query"])
        elif update.get("message"):
            _handle_message(bot, update["message"])
    except telegram.TelegramError as exc:
        logger.warning("Telegram refused an answer for bot %s: %s", bot.pk, exc)


def late_reply(log, text: str) -> bool:
    """Send a target's later answer to the chat the command came from.

    Works once per command, within 24 hours of it. Returns False when it was
    already used, is too late, has nothing to say or Telegram refused it.
    """
    text = (text or "").strip()
    if not text or log.late_replied_at is not None:
        return False
    if timezone.now() - log.created_at > LATE_REPLY_WINDOW:
        return False
    claimed = CommandLog.objects.filter(pk=log.pk, late_replied_at__isnull=True).update(late_replied_at=timezone.now())
    if not claimed:
        return False
    try:
        _send(log.bot, log.chat_id, telegram_html(text[:forward.TELEGRAM_TEXT_LIMIT]))
    except telegram.TelegramError as exc:
        logger.warning("Late reply for %s could not be sent: %s", log.public_id, exc)
        CommandLog.objects.filter(pk=log.pk).update(late_replied_at=None)  # the target may try again
        return False
    log.late_replied_at = timezone.now()
    return True


def sync_bot_menu(bot) -> None:
    """Make the bot's menu in Telegram match: built-ins plus enabled commands.

    Calls setMyCommands once per organization language, with descriptions in
    that language. The default language is sent without a language code, so it
    is also what people with any other Telegram language see.
    Raises channels.telegram.TelegramError when Telegram refuses.
    """
    client = telegram.TelegramClient(bot)
    default_code = Language.default_code()
    codes = Language.codes() or [default_code]
    for code in codes:
        with translation.override(code):
            commands = [
                {"command": name, "description": (description or f"/{name}")[:256]}
                for name, description in builtin.menu(bot, code)
            ]
        client.set_my_commands(commands, language_code=None if code == default_code else code.split("-")[0].lower())


# --------------------------------------------------------------------------- messages

def allow(bot, chat_id):
    """Count one command for this chat. Returns (allowed, count_this_minute)."""
    key = f"cmdrate:{bot.pk}:{chat_id}"
    cache.add(key, 0, RATE_WINDOW_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:  # the window ended between the two calls
        cache.set(key, 1, RATE_WINDOW_SECONDS)
        count = 1
    return count <= RATE_LIMIT, count


def _handle_message(bot, message):
    text = message.get("text") or ""
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None or not text.startswith("/"):
        return  # free-text replies are out of scope
    head = text.split(None, 1)[0]
    if "@" in head and bot.username and head.split("@", 1)[1].lower() != bot.username.lower():
        return  # addressed to another bot in a group chat
    name, arguments, _argument_list = forward.split_arguments(text)
    if not name:
        return

    link = TelegramLink.objects.filter(bot=bot, chat_id=chat_id).select_related("person", "person__language").first()
    person = link.person if link else None
    code = builtin.chat_language(bot, chat_id, person)

    allowed, count = allow(bot, chat_id)
    if not allowed:
        if count == RATE_LIMIT + 1:  # say so once, then stay quiet until the minute is over
            with translation.override(code):
                _send(bot, chat_id, builtin.esc(_("That is a lot of commands. Please wait a minute and try again.")), link)
        return

    started = time.monotonic()
    log = CommandLog.objects.create(bot=bot, person=person, chat_id=chat_id, text=text[:4096], command=name[:64])
    result, target = "answered", ""
    try:
        with translation.override(code):
            result, target = _answer(bot, chat_id, person, link, code, name, text, arguments, message, log)
    except telegram.TelegramError as exc:
        logger.warning("Could not answer /%s in chat %s: %s", name, chat_id, exc.for_staff())
        result = "error"
    except Exception:
        logger.exception("Command /%s failed", name)
        result = "error"
    log.result = result
    log.target = target or log.target
    log.duration_ms = int((time.monotonic() - started) * 1000)
    log.save(update_fields=["result", "target", "duration_ms", "updated_at"])


def _answer(bot, chat_id, person, link, code, name, text, arguments, message, log):
    """Answer one command. Returns (result, target) for the log."""
    if builtin.is_built_in(name):
        log.target = "built_in"
        reply = builtin.answer(name, bot=bot, chat_id=chat_id, person=person, link=link, arguments=arguments,
                               sender=message.get("from") or {})
        if name == "start":
            link = TelegramLink.objects.filter(bot=bot, chat_id=chat_id).first()
            if link is not None and log.person_id != link.person_id:
                log.person = link.person
                log.save(update_fields=["person", "updated_at"])
        _send(bot, chat_id, reply.html, link, buttons=reply.buttons)
        return "answered", "built_in"

    command = Command.objects.filter(bot=bot, name=name, enabled=True).first()
    if command is None:
        _send(bot, chat_id, builtin.esc(builtin.bot_text(bot, "unknown_command", code)), link)
        return "unknown", ""

    if command.connected_only and person is None:
        log.target = "built_in"
        _send(bot, chat_id, builtin.esc(_("This one is for people who are connected. Press /start to connect, then try again.")), link)
        return "answered", "built_in"

    log.target = command.target
    values = forward.build_values(name, text, person=person, bot=bot, chat_id=chat_id, log=log, language=code,
                                  first_name=(message.get("from") or {}).get("first_name", ""))
    outcome = forward.forward(command, values)
    if not outcome.ok:
        _send(bot, chat_id, builtin.esc(builtin.bot_text(bot, "no_answer", code)), link)
        return outcome.outcome, command.target
    if command.reply == "answer" and outcome.reply_text:
        _send(bot, chat_id, telegram_html(outcome.reply_text), link)
    return "answered", command.target


# --------------------------------------------------------------------------- /language buttons

def _handle_callback(bot, callback):
    data = callback.get("data") or ""
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    client = telegram.TelegramClient(bot)
    if chat_id is None or not data.startswith("lang:"):
        _quietly(client.answer_callback, callback.get("id"))
        return
    language = Language.objects.filter(code=data[5:]).first()
    if language is None:
        _quietly(client.answer_callback, callback.get("id"))
        return
    link = TelegramLink.objects.filter(bot=bot, chat_id=chat_id).select_related("person").first()
    if link is not None:
        person = link.person
        if person.language_id != language.pk:
            person.language = language
            person.save(update_fields=["language", "updated_at"])
    else:
        builtin.remember_chat_language(bot, chat_id, language.code)
    with translation.override(language.code):
        saved = _("Saved. I will write to you in %(language)s.") % {"language": language.name}
    _quietly(client.answer_callback, callback.get("id"), saved)
    _send(bot, chat_id, builtin.esc(saved), link)


def _quietly(call, *args):
    try:
        call(*args)
    except telegram.TelegramError:
        pass


# --------------------------------------------------------------------------- sending

def _send(bot, chat_id, html, link=None, buttons=None):
    """Send to a chat. When Telegram says the person blocked the bot, mark the link."""
    try:
        return telegram.TelegramClient(bot).send(chat_id, html, buttons=buttons)
    except telegram.TelegramError as exc:
        if exc.blocked:
            TelegramLink.objects.filter(bot=bot, chat_id=chat_id, blocked_at__isnull=True).update(blocked_at=timezone.now())
        raise
