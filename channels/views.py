"""The Channels screen (spec section 6): email senders, Telegram bots, push apps, webhooks. Owner only."""
import hmac
import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.fields import mask
from core.models import Building, Organization
from core.permissions import role_required

from . import push, telegram, webhooks
from .email import test_connection
from .models import EmailSender, PushApp, TelegramBot, Webhook

log = logging.getLogger(__name__)
GMAIL = {"host": "smtp.gmail.com", "port": 587, "tls_mode": "starttls"}


# --------------------------------------------------------------------------- the screen

def _index(request, error_key="", error="", values=None, reveal=None, status=200):
    from delivery import limits
    from delivery.models import Message

    org = Organization.get()
    day = limits.local_day_start(org)
    month = day.replace(day=1)

    senders = list(EmailSender.objects.annotate(
        sent_month=Count("message", filter=Q(message__state__in=Message.DELIVERED_STATES, message__sent_at__gte=month))))
    for sender in senders:
        sender.status = limits.sender_status(sender)
        sender.masked_password = mask(sender.password)
        sender.form_key = f"email_{sender.public_id}"
    app_sender = next((s for s in senders if s.purpose == "app"), None)
    buildings = list(Building.objects.all())

    bots = list(TelegramBot.objects.annotate(
        people=Count("links", filter=Q(links__blocked_at__isnull=True), distinct=True),
        own_commands=Count("commands", distinct=True)))
    for bot in bots:
        bot.failed_today = Message.objects.filter(bot=bot, state="failed", updated_at__gte=day).count()
        bot.form_key = f"telegram_{bot.public_id}"

    push_buildings = []
    for app in PushApp.objects.select_related("building", "list").annotate(people=Count("members")).order_by("building__name", "id"):
        app.form_key = f"push_{app.public_id}"
        app.host = app.endpoint.split("://", 1)[-1]
        if push_buildings and push_buildings[-1]["building"].pk == app.building_id:
            push_buildings[-1]["apps"].append(app)
        else:
            base = (app.building.base_url or app.endpoint).split("://", 1)[-1].split("/", 1)[0]
            push_buildings.append({"building": app.building, "address": base, "apps": [app]})

    hooks = list(Webhook.objects.order_by("id"))
    for hook in hooks:
        hook.form_key = f"webhook_{hook.public_id}"

    return render(request, "channels/index.html", {
        "app_sender": app_sender,
        "newsletter_senders": [s for s in senders if s.purpose == "newsletter"],
        "buildings": buildings,
        "bots": bots, "push_buildings": push_buildings, "webhooks": hooks,
        "telegram_mode": settings.TELEGRAM_MODE,
        "error_key": error_key, "error": error, "values": values or {}, "reveal": reveal or {},
        "tls_choices": EmailSender.TLS_CHOICES,
    }, status=status)


@role_required("owner")
def index(request):
    return _index(request)


def _back():
    return redirect("channels:index")


# --------------------------------------------------------------------------- email senders

def _read_sender(post, sender):
    """Fill a sender from the form. Returns a list of problems, in plain words."""
    problems = []
    kind = post.get("kind") if post.get("kind") in ("gmail", "smtp") else (sender.kind if sender.pk else "gmail")
    sender.kind = kind
    sender.from_name = (post.get("from_name") or "").strip()
    sender.from_email = (post.get("from_email") or "").strip().lower()
    if not sender.from_name:
        problems.append(_("Enter the name people see."))
    try:
        validate_email(sender.from_email)
    except ValidationError:
        problems.append(_("Enter the email address this sends from."))
    password = post.get("password") or ""
    if password:
        sender.password = password
    elif not sender.password:
        problems.append(_("Enter the app password.") if kind == "gmail" else _("Enter the password."))
    if kind == "gmail":
        sender.host, sender.port, sender.tls_mode = GMAIL["host"], GMAIL["port"], GMAIL["tls_mode"]
        sender.username = sender.from_email
    else:
        sender.host = (post.get("host") or "").strip()
        try:
            sender.port = int(post.get("port") or 587)
        except ValueError:
            problems.append(_("The port is a number, usually 587 or 465."))
        sender.tls_mode = post.get("tls_mode") if post.get("tls_mode") in dict(EmailSender.TLS_CHOICES) else "starttls"
        sender.username = (post.get("username") or "").strip() or sender.from_email
        if not sender.host:
            problems.append(_("Enter the mail server's address."))
    daily = (post.get("daily_limit") or "").strip()
    try:
        sender.daily_limit = int(daily) if daily else None
        if sender.daily_limit is not None and sender.daily_limit < 1:
            raise ValueError
    except ValueError:
        problems.append(_("The daily limit is a whole number, or empty for no limit."))
    try:
        sender.per_second = max(1, int(post.get("per_second") or 5))
    except ValueError:
        sender.per_second = 5
    return problems


def _test_and_save(request, sender, form_key):
    """Save only when the mail server lets us in; otherwise show its error."""
    problems = _read_sender(request.POST, sender)
    if not problems:
        ok, error = test_connection(sender)
        if not ok:
            problems.append(_("The mail server did not let us in, so nothing was saved. It said: %(error)s") % {"error": error})
    if problems:
        values = request.POST.dict()
        values.pop("password", None)
        return _index(request, form_key, " ".join(problems), values)
    sender.state, sender.last_error = "ok", ""
    if sender.purpose == "newsletter" and not EmailSender.objects.filter(purpose="newsletter", is_default=True).exclude(pk=sender.pk).exists():
        sender.is_default = True
    sender.save()
    messages.success(request, _("%(address)s works and is saved.") % {"address": sender.from_email})
    return _back()


@role_required("owner")
@require_POST
def sender_add(request):
    purpose = "app" if request.POST.get("purpose") == "app" else "newsletter"
    if purpose == "app":
        # There is exactly one app sender; adding another replaces it.
        sender = EmailSender.app_sender() or EmailSender(purpose="app")
        if sender.pk:
            sender.password = ""
    else:
        sender = EmailSender(purpose="newsletter")
    return _test_and_save(request, sender, f"email_add_{purpose}")


def _sender(public_id):
    return EmailSender.get_by_public_id(public_id)


@role_required("owner")
@require_POST
def sender_edit(request, public_id):
    sender = _sender(public_id)
    return _test_and_save(request, sender, f"email_{sender.public_id}")


@role_required("owner")
@require_POST
def sender_test(request, public_id):
    """"Send a test" emails the signed-in team member."""
    from content.render import RenderedEmail, wrap_email
    from delivery.pipeline import send_test_email

    sender = _sender(public_id)
    org = Organization.get()
    line = _("This is a test from Post Office. If you can read it, %(address)s is set up correctly.") % {"address": sender.from_email}
    row = (f'<tr><td style="padding:10px 32px;font-family:Helvetica,Arial,sans-serif;font-size:16px;line-height:1.55;'
           f'color:#1B1F2A">{escape(line)}</td></tr>')
    rendered = RenderedEmail(subject=_("Test from Post Office"),
                             html=wrap_email(row, org=org, lang=request.LANGUAGE_CODE or "en"), text=line)
    ok, error = send_test_email(sender, request.user.email, rendered)
    if ok:
        EmailSender.objects.filter(pk=sender.pk).update(state="ok", last_error="")
        messages.success(request, _("Test sent to %(address)s.") % {"address": request.user.email})
    else:
        EmailSender.objects.filter(pk=sender.pk).update(state="error", last_error=error)
        messages.error(request, _("The test did not go out. The mail server said: %(error)s") % {"error": error})
    return _back()


@role_required("owner")
@require_POST
def sender_default(request, public_id):
    sender = _sender(public_id)
    if sender.purpose == "newsletter":
        EmailSender.objects.filter(purpose="newsletter").update(is_default=False)
        EmailSender.objects.filter(pk=sender.pk).update(is_default=True)
        messages.success(request, _("%(name)s is now the default for newsletters.") % {"name": sender.from_name})
    return _back()


@role_required("owner")
@require_POST
def sender_delete(request, public_id):
    sender = _sender(public_id)
    if sender.purpose == "app" and Building.objects.exists():
        messages.error(request, _("Apps are connected and send through this address. Change the sender instead of removing it."))
        return _back()
    was_default = sender.is_default and sender.purpose == "newsletter"
    sender.delete()
    if was_default:
        nxt = EmailSender.objects.filter(purpose="newsletter").first()
        if nxt:
            EmailSender.objects.filter(pk=nxt.pk).update(is_default=True)
    messages.success(request, _("Sender removed."))
    return _back()


# --------------------------------------------------------------------------- Telegram bots

def webhook_url(bot):
    return f"{settings.PUBLIC_URL}/tg/{bot.public_id}/{bot.webhook_secret}"


def _token_problem(error):
    if error.code in (401, 404):
        return _("Telegram does not know this token. Copy it again from BotFather.")
    if error.code >= 500:
        return _("Could not reach Telegram just now. Try again in a minute.")
    return error.for_staff()


def _register_webhook(request, bot):
    if settings.TELEGRAM_MODE != "webhook":
        return
    try:
        telegram.TelegramClient(bot).set_webhook(webhook_url(bot))
    except telegram.TelegramError as error:
        messages.error(request, _("The bot is saved, but Telegram did not accept our address for updates (%(error)s).")
                       % {"error": error.description})


@role_required("owner")
@require_POST
def bot_add(request):
    token = (request.POST.get("token") or "").strip()
    if not token:
        return _index(request, "telegram_add", _("Paste the token from BotFather."))
    try:
        me = telegram.TelegramClient(token=token).get_me() or {}
    except telegram.TelegramError as error:
        return _index(request, "telegram_add", _token_problem(error))
    username = me.get("username") or ""
    bot = TelegramBot.objects.filter(username__iexact=username).first() if username else None
    if bot is None:
        bot = TelegramBot(is_default=not TelegramBot.objects.exists())
    bot.username = username
    bot.name = bot.name or me.get("first_name") or username or _("Telegram bot")
    bot.token = token
    bot.save()
    _register_webhook(request, bot)
    messages.success(request, _("@%(username)s is connected.") % {"username": username})
    return _back()


def _bot(public_id):
    return TelegramBot.get_by_public_id(public_id)


@role_required("owner")
@require_POST
def bot_edit(request, public_id):
    bot = _bot(public_id)
    name = (request.POST.get("name") or "").strip()
    token = (request.POST.get("token") or "").strip()
    if not name:
        return _index(request, bot_key(bot), _("Give the bot a name."), request.POST.dict() | {"token": ""})
    if token:
        try:
            me = telegram.TelegramClient(token=token).get_me() or {}
        except telegram.TelegramError as error:
            return _index(request, bot_key(bot), _token_problem(error), {"name": name})
        bot.token = token
        bot.username = me.get("username") or bot.username
    bot.name = name
    bot.save()
    if token:
        _register_webhook(request, bot)
    messages.success(request, _("Saved."))
    return _back()


def bot_key(bot):
    return f"telegram_{bot.public_id}"


@role_required("owner")
@require_POST
def bot_default(request, public_id):
    bot = _bot(public_id)
    TelegramBot.objects.update(is_default=False)
    TelegramBot.objects.filter(pk=bot.pk).update(is_default=True)
    messages.success(request, _("%(name)s is now the default bot.") % {"name": bot.name})
    return _back()


@role_required("owner")
@require_POST
def bot_delete(request, public_id):
    bot = _bot(public_id)
    was_default = bot.is_default
    if settings.TELEGRAM_MODE == "webhook":
        try:
            telegram.TelegramClient(bot).delete_webhook()
        except telegram.TelegramError:
            pass
    bot.delete()
    if was_default:
        nxt = TelegramBot.objects.first()
        if nxt:
            TelegramBot.objects.filter(pk=nxt.pk).update(is_default=True)
    messages.success(request, _("Bot removed. People who were connected to it will need to connect again if you add it back."))
    return _back()


# --------------------------------------------------------------------------- push apps

@role_required("owner")
@require_POST
def push_connect(request):
    address = (request.POST.get("address") or "").strip()
    try:
        building, apps, created = push.connect_app(address)
    except push.PushError as error:
        return _index(request, "push_add", error.reason, {"address": address})
    labels = ", ".join(app.label for app in apps)
    messages.success(request, _("%(name)s is connected: %(labels)s. A list was made for each.") % {"name": building.name, "labels": labels})
    return _back()


def _push_app(public_id):
    return PushApp.get_by_public_id(public_id)


@role_required("owner")
@require_POST
def push_edit(request, public_id):
    app = _push_app(public_id)
    label = (request.POST.get("label") or "").strip()
    endpoint = (request.POST.get("endpoint") or "").strip()
    try:
        URLValidator(schemes=["http", "https"])(endpoint)
    except ValidationError:
        return _index(request, app_key(app), _("Enter the full address notifications are sent to, starting with https://."), request.POST.dict() | {"secret": ""})
    if not label:
        return _index(request, app_key(app), _("Give this audience a name."), request.POST.dict() | {"secret": ""})
    app.label, app.endpoint = label, endpoint
    if (request.POST.get("secret") or "").strip():
        app.secret = request.POST["secret"].strip()
    app.save()
    messages.success(request, _("Saved."))
    return _back()


def app_key(app):
    return f"push_{app.public_id}"


@role_required("owner")
@require_POST
def push_test(request, public_id):
    """Post an empty, signed batch to check the address and the shared secret."""
    app = _push_app(public_id)
    try:
        push.send_batch(app, [], f"test:{uuid.uuid4().hex[:12]}")
    except push.PushError as error:
        messages.error(request, _("The test did not work: %(reason)s.") % {"reason": error.reason})
    else:
        messages.success(request, _("%(name)s answered. The address and the shared secret work.") % {"name": app.building.name})
    return _back()


@role_required("owner")
@require_POST
def push_secret(request, public_id):
    app = _push_app(public_id)
    return _index(request, reveal={app_key(app): app.secret})


@role_required("owner")
@require_POST
def push_delete(request, public_id):
    app = _push_app(public_id)
    app.delete()
    messages.success(request, _("Disconnected. Its list is kept; remove it from People if you no longer need it."))
    return _back()


# --------------------------------------------------------------------------- webhooks

@role_required("owner")
@require_POST
def webhook_add(request):
    name = (request.POST.get("name") or "").strip()
    url = (request.POST.get("url") or "").strip()
    values = {"name": name, "url": url}
    if not name:
        return _index(request, "webhook_add", _("Give the webhook a name, for example Lobby display."), values)
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        return _index(request, "webhook_add", _("Enter the full address, starting with https://."), values)
    Webhook.objects.create(name=name, url=url)
    messages.success(request, _("Webhook added. It gets a copy of every campaign from now on."))
    return _back()


def _hook(public_id):
    return Webhook.get_by_public_id(public_id)


@role_required("owner")
@require_POST
def webhook_toggle(request, public_id):
    hook = _hook(public_id)
    hook.enabled = request.POST.get("enabled") in ("1", "on", "true")
    hook.save(update_fields=["enabled", "updated_at"])
    messages.success(request, _("%(name)s is on.") % {"name": hook.name} if hook.enabled else _("%(name)s is off.") % {"name": hook.name})
    return _back()


@role_required("owner")
@require_POST
def webhook_test(request, public_id):
    hook = _hook(public_id)
    ok, status = webhooks.post_to(hook, {"event": "test", "name": _("Test from Post Office"),
                                         "sent_at": timezone.now().isoformat(), "lists": [], "content": {}})
    if ok:
        messages.success(request, _("%(name)s answered.") % {"name": hook.name})
    else:
        messages.error(request, _("%(name)s did not accept the test (%(status)s).") % {"name": hook.name, "status": status})
    return _back()


@role_required("owner")
@require_POST
def webhook_secret(request, public_id):
    hook = _hook(public_id)
    return _index(request, reveal={f"webhook_{hook.public_id}": hook.secret})


@role_required("owner")
@require_POST
def webhook_delete(request, public_id):
    _hook(public_id).delete()
    messages.success(request, _("Webhook removed."))
    return _back()


# --------------------------------------------------------------------------- Telegram webhook (public, no sign-in)

@csrf_exempt
@require_POST
def telegram_webhook(request, bot_id, secret):
    """/tg/<bot public id>/<webhook secret>: Telegram posts updates here in webhook mode."""
    pk = TelegramBot.pk_from_public_id(bot_id)
    bot = TelegramBot.objects.filter(pk=pk).first() if pk else None
    if bot is None or not hmac.compare_digest(secret.encode(), bot.webhook_secret.encode()):
        raise Http404("Unknown bot")
    try:
        update = json.loads(request.body or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    if isinstance(update, dict):
        from .tasks import handle_update

        handle_update(bot, update)
        update_id = update.get("update_id")
        if isinstance(update_id, int) and update_id > bot.last_update_id:
            TelegramBot.objects.filter(pk=bot.pk).update(last_update_id=update_id)
    # Always 200: Telegram would otherwise send the same update again and again.
    return HttpResponse("ok")
