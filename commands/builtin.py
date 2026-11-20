"""The built-in commands: /start, /stop, /language, /help (spec section 12).

Always on, answered by Post Office itself, in the person's language. Every
function here runs inside translation.override(<the person's language>) and
returns what to send: a Reply (Telegram HTML plus optional inline buttons).
"""
import html
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from channels.models import TelegramLink
from core.models import Language, tr
from people import services, tokens
from people.models import List, Subscription

from .models import BUILT_IN_COMMANDS, BotText, Command

# What each built-in does, for Telegram's menu and /help (readers see these).
MENU_DESCRIPTIONS = {
    "start": gettext_lazy("Connect this chat to your subscription"),
    "stop": gettext_lazy("Stop newsletters here"),
    "language": gettext_lazy("Change your language"),
    "help": gettext_lazy("See what I can do"),
}

# The same, worded for staff on the Commands screen.
STAFF_DESCRIPTIONS = {
    "start": gettext_lazy("Connects their Telegram to their subscription"),
    "stop": gettext_lazy("Stops newsletters on Telegram"),
    "language": gettext_lazy("Lets them change their language"),
    "help": gettext_lazy("Lists your commands with their descriptions"),
}

DEFAULT_TEXTS = {
    "unknown_command": gettext_lazy("Sorry, I do not know that one. Type /help to see what I can do."),
    "no_answer": gettext_lazy("Sorry, that is not working right now. Please try again later."),
}

CHAT_LANGUAGE_SECONDS = 60 * 60 * 24 * 90


@dataclass
class Reply:
    html: str
    buttons: list | None = None


def esc(value):
    return html.escape(str(value or ""), quote=False)


# --------------------------------------------------------------------------- languages and texts

def _chat_language_key(bot, chat_id):
    return f"tgchatlang:{bot.pk}:{chat_id}"


def chat_language(bot, chat_id, person=None):
    """The language to answer in: the person's, else what this chat picked with /language, else the default."""
    if person is not None:
        return person.language_code
    code = cache.get(_chat_language_key(bot, chat_id))
    if code and Language.objects.filter(code=code).exists():
        return code
    return Language.default_code()


def remember_chat_language(bot, chat_id, code):
    cache.set(_chat_language_key(bot, chat_id), code, CHAT_LANGUAGE_SECONDS)


def default_bot_text(kind):
    """The shipped wording for unknown_command / no_answer, in the active language."""
    return str(DEFAULT_TEXTS[kind])


def bot_text(bot, kind, code):
    """The bot's own text in a language; the default language's, then the shipped wording, when empty.

    Call inside translation.override(code).
    """
    rows = {row.language: getattr(row, kind) for row in BotText.objects.filter(bot=bot)}
    if (rows.get(code) or "").strip():
        return rows[code].strip()
    default_code = Language.default_code()
    builtin_in_this_language = code.split("-")[0] in settings.SHIPPED_TRANSLATIONS
    if not builtin_in_this_language and (rows.get(default_code) or "").strip():
        return rows[default_code].strip()
    return default_bot_text(kind)


def menu(bot, code):
    """[(name, description)] for Telegram's menu and /help, built-ins first.

    Call inside translation.override(code).
    """
    items = [(name, str(MENU_DESCRIPTIONS[name])) for name in BUILT_IN_COMMANDS]
    for command in Command.objects.filter(bot=bot, enabled=True):
        items.append((command.name, tr(command.descriptions, code) or f"/{command.name}"))
    return items


# --------------------------------------------------------------------------- the commands

def is_built_in(name):
    return name in BUILT_IN_COMMANDS


def answer(name, *, bot, chat_id, person, link, arguments, sender=None):
    """Run a built-in command. Returns a Reply."""
    handler = {"start": _start, "stop": _stop, "language": _language, "help": _help}[name]
    return handler(bot=bot, chat_id=chat_id, person=person, link=link, arguments=arguments, sender=sender or {})


def _start(*, bot, chat_id, person, link, arguments, sender):
    token = arguments.split()[0] if arguments.strip() else ""
    if token:
        named = tokens.person_from_connect_token(token)
        if named is None:
            return Reply(esc(_("This link has already been used or is too old. Open your preferences page to get a new one, "
                               "or sign up here: %(url)s") % {"url": signup_url()}))
        connect(named, bot, chat_id, username=sender.get("username") or "")
        hello = (_("You are connected, %(name)s.") % {"name": named.first_name}) if named.first_name else _("You are connected.")
        return Reply(esc(hello + " " + _("Messages you signed up for will arrive here. Choose what you get: %(url)s")
                         % {"url": tokens.preferences_url(named)}))
    if person is not None:
        # /start again (for example after /stop) restores nothing by itself: it shows the preferences link.
        if link is not None and link.blocked_at is not None:
            link.blocked_at = None
            link.save(update_fields=["blocked_at", "updated_at"])
        return Reply(esc(_("You are already connected. Choose what you get here: %(url)s")
                         % {"url": tokens.preferences_url(person)}))
    return Reply(esc(_("Welcome. To get messages here, sign up first: %(url)s") % {"url": signup_url()}))


def _stop(*, bot, chat_id, person, link, arguments, sender):
    if person is None:
        return Reply(esc(_("This chat is not connected, so there is nothing to stop.")))
    services.unsubscribe_all_news(person, channel="telegram")
    return Reply(esc(_("Done. You will not get newsletters here any more. Messages about things you signed up for still arrive. "
                       "Change your mind any time: %(url)s") % {"url": tokens.preferences_url(person)}))


def _language(*, bot, chat_id, person, link, arguments, sender):
    return Reply(esc(_("Which language would you like?")), buttons=language_buttons())


def _help(*, bot, chat_id, person, link, arguments, sender):
    code = chat_language(bot, chat_id, person)
    lines = [esc(_("Here is what I can do:")), ""]
    lines += [f"/{name} - {esc(description)}" for name, description in menu(bot, code)]
    return Reply("\n".join(lines))


def language_buttons():
    """One button per organization language, two to a row."""
    buttons = [{"text": lang.name, "callback_data": f"lang:{lang.code}"} for lang in Language.objects.all()]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def signup_url():
    return f"{settings.PUBLIC_URL}/join"


def connect(person, bot, chat_id, username=""):
    """Link a chat to a person and switch Telegram on where they already agreed to hear from us.

    App lists the person is on get Telegram switched on. Newsletters are never
    switched on without consent: a campaign list is mirrored to Telegram only
    when its email subscription is on and carries a consent record. A Telegram
    choice the person already made (for example with /stop) is left alone.
    """
    link, _created = TelegramLink.objects.update_or_create(
        bot=bot, chat_id=chat_id, defaults={"person": person, "username": username[:120], "blocked_at": None},
    )
    decided = set(Subscription.objects.filter(person=person, channel="telegram").values_list("list_id", flat=True))
    on_app_lists = List.objects.filter(kind="app", subscriptions__person=person, subscriptions__state="on").distinct()
    for lst in on_app_lists:
        if lst.pk not in decided:
            services.set_subscription(person, lst, "telegram", True)
    consented = Subscription.objects.filter(
        person=person, channel="email", state="on", list__kind="campaign", consent__isnull=False,
    ).select_related("list", "consent")
    for sub in consented:
        if sub.list_id not in decided:
            services.set_subscription(person, sub.list, "telegram", True, consent=sub.consent)
    return link
