"""The Commands screen: what each Telegram bot does with a slash command (owner only)."""
import json
import re
from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from channels import telegram
from channels.models import TelegramBot
from content.render import telegram_html
from core.fields import mask
from core.models import Language, Organization
from core.permissions import role_required
from people.models import Person

from . import builtin, forward
from .handler import sync_bot_menu
from .models import BUILT_IN_COMMANDS, BotText, Command, CommandLog

NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
LOGS_PER_PAGE = 10
MASK = mask("x")


# --------------------------------------------------------------------------- helpers

def _pick_bot(request):
    """The bot chosen with ?bot=<public id> (or the posted field), else the default bot."""
    public_id = request.POST.get("bot") or request.GET.get("bot") or ""
    pk = TelegramBot.pk_from_public_id(public_id)
    bot = TelegramBot.objects.filter(pk=pk).first() if pk is not None else None
    return bot or TelegramBot.default()


def _index_url(bot, command=None, new=False):
    url = f"{reverse('commands:index')}?bot={bot.public_id}"
    if command is not None:
        url += f"&cmd={command.public_id}"
    elif new:
        url += "&new=1"
    return url


def _sync(request, bot):
    """Update the bot's menu in Telegram. A failure is a notice, never a failed save."""
    try:
        sync_bot_menu(bot)
    except telegram.TelegramError as exc:
        messages.error(request, _("Saved, but the bot's menu in Telegram could not be updated: %(reason)s. "
                                  "It will be tried again the next time you change a command.") % {"reason": exc.for_staff()})
        return False
    except Exception:
        messages.error(request, _("Saved, but the bot's menu in Telegram could not be updated."))
        return False
    return True


def _header_rows(request):
    names = request.POST.getlist("header_name")
    values = request.POST.getlist("header_value")
    origs = request.POST.getlist("header_orig")
    rows = []
    for i, name in enumerate(names):
        orig = origs[i] if i < len(origs) else ""
        rows.append({
            "name": name.strip(),
            "value": values[i] if i < len(values) else "",
            "orig": int(orig) if orig.isdigit() else None,
        })
    return rows


def _existing_command(request, bot):
    pk = Command.pk_from_public_id(request.POST.get("id") or "")
    return Command.objects.filter(pk=pk, bot=bot).first() if pk is not None else None


def _read_form(request, bot):
    """Read the editor form. Returns (command, errors, rows, existing).

    `command` is the existing row with the posted values applied (not saved),
    or a new unsaved Command. `rows` are the header rows as typed.
    """
    existing = _existing_command(request, bot)
    command = existing or Command(bot=bot)
    saved_headers = list(command.headers or [])
    rows = _header_rows(request)
    errors = {}

    name = request.POST.get("name", "").strip().lstrip("/").lower()
    if not NAME_RE.match(name):
        errors["name"] = _("Use lowercase letters, numbers and underscores, up to 32 characters.")
    elif name in BUILT_IN_COMMANDS:
        errors["name"] = _("/%(name)s is built in. Choose another name.") % {"name": name}
    elif Command.objects.filter(bot=bot, name=name).exclude(pk=command.pk).exists():
        errors["name"] = _("This bot already has /%(name)s.") % {"name": name}
    command.name = name[:32]

    command.descriptions = {
        code: request.POST.get(f"desc_{code}", "").strip()[:256]
        for code in Language.codes() if request.POST.get(f"desc_{code}", "").strip()
    }
    method = request.POST.get("method", "POST").upper()
    command.method = method if method in forward.METHODS else "POST"
    command.target = "http"  # TODO(switchboard): read the chosen target here when there is a second one
    command.url = request.POST.get("url", "").strip()
    if not command.url:
        errors["url"] = _("Enter the address to pass the command to.")
    elif not re.match(r"^https?://[^\s/]+", command.url):
        errors["url"] = _("The address must start with https:// or http://.")
    command.headers = forward.store_headers(rows, saved_headers)
    command.body = "" if command.method in forward.NO_BODY_METHODS else request.POST.get("body", "")
    command.reply_path = request.POST.get("reply_path", "").strip()[:200]
    command.reply = "none" if request.POST.get("reply") == "none" else "answer"
    command.connected_only = "connected_only" in request.POST
    command.enabled = "enabled" in request.POST
    return command, errors, rows, existing


def _form_state(command, rows=None):
    """What the editor's Alpine component starts from. Saved header values never leave the server."""
    if rows is None:
        headers = [{"name": h.get("name", ""), "value": "", "orig": i, "saved": True}
                   for i, h in enumerate(command.headers or [])]
    else:
        headers = [{
            "name": r["name"],
            "value": "" if r["value"] == MASK else r["value"],
            "orig": r["orig"],
            "saved": r["orig"] is not None,
        } for r in rows]
    return {
        "name": command.name or "",
        "method": command.method or "POST",
        "url": command.url or "",
        "body": command.body or "",
        "headers": headers,
        "defaultBody": forward.DEFAULT_BODY,
    }


def _preview_text(command, rows, values):
    """The exact request as text. Values typed just now are shown; saved ones stay masked."""
    request = forward.build_request(command, values)
    typed = {r["name"]: r["value"] for r in rows if r["name"] and r["value"] not in ("", MASK)}
    lines = [f"{request.method} {request.url}"]
    for name in request.headers:
        lines.append(f"{name}: {typed.get(name, MASK)}")
    if request.body:
        lines += ["", request.body]
    return "\n".join(lines)


def _saved_rows(command):
    return [{"name": h.get("name", ""), "value": "", "orig": i} for i, h in enumerate(command.headers or [])]


def _example_answer(path):
    answer = "Thanks, Taras. We passed it on."
    if not path:
        return answer
    for part in reversed(path.split(".")):
        answer = [answer] if part.isdigit() else {part: answer}
    return json.dumps(answer, ensure_ascii=False)


def _preview_context(command, rows, bot):
    values = forward.sample_values(command.name, bot)
    path = (command.reply_path or "").strip()
    return {
        "preview": _preview_text(command, rows, values),
        "sample_text": values["text"],
        "reply_path": path,
        "example_answer": _example_answer(path),
        "sends_reply": command.reply != "none",
    }


def _context(request, bot, editing=None, errors=None, rows=None, is_new=False):
    languages = list(Language.objects.all())
    default_code = Language.default_code()
    since = timezone.now() - timedelta(days=30)
    used = dict(CommandLog.objects.filter(bot=bot, created_at__gte=since).order_by().values_list("command")
                .annotate(n=Count("id")))
    commands = list(Command.objects.filter(bot=bot))
    for command in commands:
        command.used = used.get(command.name, 0)
        command.description = (command.descriptions or {}).get(default_code) or next(
            (v for v in (command.descriptions or {}).values() if v), "")
        command.short_url = re.sub(r"^https?://", "", command.url or "").split("?")[0]

    saved_texts = {t.language: t for t in BotText.objects.filter(bot=bot)}
    texts = []
    for lang in languages:
        with translation.override(lang.code):
            texts.append({
                "lang": lang,
                "unknown_command": getattr(saved_texts.get(lang.code), "unknown_command", ""),
                "no_answer": getattr(saved_texts.get(lang.code), "no_answer", ""),
                "default_unknown": builtin.default_bot_text("unknown_command"),
                "default_no_answer": builtin.default_bot_text("no_answer"),
            })

    page = Paginator(CommandLog.objects.filter(bot=bot).select_related("person"), LOGS_PER_PAGE).get_page(
        request.GET.get("page"))
    tz = Organization.get().tz
    for log in page:
        log.when = date_format(log.created_at.astimezone(tz), "M j, g:i A")
        log.seconds = f"{log.duration_ms / 1000:.1f}"

    ctx = {
        "bots": list(TelegramBot.objects.all()),
        "bot": bot,
        "commands": commands,
        "builtins": [(name, builtin.STAFF_DESCRIPTIONS[name]) for name in BUILT_IN_COMMANDS],
        "languages": languages,
        "default_code": default_code,
        "texts": texts,
        "logs": page,
        "editing": editing,
        "is_new": is_new,
        "errors": errors or {},
        "methods": forward.METHODS,
        "placeholders": [{"token": "{{%s}}" % key, "key": key.split(".")[-1], "label": label}
                         for key, label, _sample in forward.PLACEHOLDERS],
    }
    if editing is not None:
        shown_rows = rows if rows is not None else _saved_rows(editing)
        ctx["form_state"] = _form_state(editing, rows)
        ctx["descriptions"] = [{"lang": lang, "value": (editing.descriptions or {}).get(lang.code, "")} for lang in languages]
        ctx.update(_preview_context(editing, shown_rows, bot))
    return ctx


# --------------------------------------------------------------------------- pages

@role_required("owner")
def index(request):
    bot = _pick_bot(request)
    if bot is None:
        return render(request, "commands/index.html", {"bot": None, "bots": []})
    editing, is_new = None, False
    if request.GET.get("new"):
        editing, is_new = Command(bot=bot, method="POST", body=forward.DEFAULT_BODY, reply_path="reply"), True
    else:
        pk = Command.pk_from_public_id(request.GET.get("cmd") or "")
        if pk is not None:
            editing = Command.objects.filter(pk=pk, bot=bot).first()
        if editing is None:
            editing = Command.objects.filter(bot=bot).first()
    return render(request, "commands/index.html", _context(request, bot, editing, is_new=is_new))


@role_required("owner")
@require_POST
def save(request):
    bot = _pick_bot(request)
    if bot is None:
        return redirect("commands:index")
    command, errors, rows, existing = _read_form(request, bot)
    if errors:
        ctx = _context(request, bot, command, errors=errors, rows=rows, is_new=existing is None)
        return render(request, "commands/index.html", ctx, status=422)
    command.save()
    if _sync(request, bot):
        messages.success(request, _("/%(name)s is saved.") % {"name": command.name})
    return redirect(_index_url(bot, command))


@role_required("owner")
@require_POST
def delete(request, command_id):
    command = Command.get_by_public_id(command_id)
    bot, name = command.bot, command.name
    command.delete()
    if _sync(request, bot):
        messages.success(request, _("/%(name)s is deleted.") % {"name": name})
    return redirect(_index_url(bot))


@role_required("owner")
@require_POST
def toggle(request, command_id):
    command = Command.get_by_public_id(command_id)
    command.enabled = "enabled" in request.POST
    command.save(update_fields=["enabled", "updated_at"])
    if _sync(request, command.bot):
        if command.enabled:
            messages.success(request, _("/%(name)s is on.") % {"name": command.name})
        else:
            messages.success(request, _("/%(name)s is off.") % {"name": command.name})
    editing = Command.pk_from_public_id(request.POST.get("editing") or "")
    back = Command.objects.filter(pk=editing, bot=command.bot).first() if editing else None
    return redirect(_index_url(command.bot, back))


@role_required("owner")
@require_POST
def texts(request):
    """The bot's own texts (unknown command, no answer), one pair per language."""
    bot = _pick_bot(request)
    if bot is None:
        return redirect("commands:index")
    for code in Language.codes():
        unknown = request.POST.get(f"unknown_{code}", "").strip()
        no_answer = request.POST.get(f"no_answer_{code}", "").strip()
        BotText.objects.update_or_create(bot=bot, language=code,
                                         defaults={"unknown_command": unknown, "no_answer": no_answer})
    messages.success(request, _("The bot's texts are saved."))
    return redirect(_index_url(bot) + "#bc-texts")


@role_required("owner")
@require_POST
def preview(request):
    """The live preview: the exact request for a sample message, from the form as it stands."""
    bot = _pick_bot(request)
    if bot is None:
        return redirect("commands:index")
    command, _errors, rows, _existing = _read_form(request, bot)
    return render(request, "commands/_preview.html", _preview_context(command, rows, bot))


@role_required("owner")
@require_POST
def test(request):
    """Send a test: fire the request as it stands, with the signed-in team member as the person."""
    bot = _pick_bot(request)
    if bot is None:
        return redirect("commands:index")
    command, errors, _rows, _existing = _read_form(request, bot)
    if "url" in errors:
        return render(request, "commands/_test_result.html", {"problem": errors["url"]})
    member = request.user
    person = Person.objects.filter(email=member.email.lower()).first()
    link = person.telegram_links.filter(bot=bot).first() if person else None
    name = command.name or "test"
    values = forward.build_values(
        name, f"/{name} test", person=person, bot=bot, chat_id=link.chat_id if link else 0,
        first_name=member.first_name, language=Language.default_code(),
    )
    result = forward.forward(command, values)
    return render(request, "commands/_test_result.html", {
        "result": result,
        "seconds": result.duration_ms / 1000,
        "reply_html": telegram_html(result.reply_text) if result.ok and result.reply_text else "",
        "sends_reply": command.reply != "none",
        "first_name": member.first_name,
    })
