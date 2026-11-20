"""The HTTP API under /api/v1/ (spec section 14).

Buildings authenticate with their building key; anything else uses an API key
with a scope. Errors are {"error": {"code": "...", "message": "..."}}.
"""
import datetime
import hmac
import ipaddress
import time

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from rest_framework.decorators import api_view, authentication_classes, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle

from channels.models import PushApp, PushMember
from commands.models import CommandLog
from content.models import Template
from core.models import Language
from delivery.models import SendRequest
from people.models import List, Person, PersonExternalId, Subscription
from people.services import ConsentRequired, find_or_create_person, find_person, record_consent, set_subscription
from people.tokens import preferences_url

from .errors import ApiError

RATE_PER_SECOND = 60


class KeyRateThrottle(BaseThrottle):
    """60 requests per second per key, counted in the Django cache."""

    def allow_request(self, request, view):
        principal = request.user
        if principal is None or not getattr(principal, "rate_key", None):
            return True
        bucket = f"po:api:{principal.rate_key}:{int(time.time())}"
        cache.add(bucket, 0, 5)
        try:
            used = cache.incr(bucket)
        except ValueError:
            cache.set(bucket, 1, 5)
            used = 1
        return used <= RATE_PER_SECOND

    def wait(self):
        return 1


def endpoint(methods):
    """An API view with the per-key rate limit."""
    def decorator(view):
        return api_view(methods)(throttle_classes([KeyRateThrottle])(view))

    return decorator


# --------------------------------------------------------------------------- who is calling

def _principal(request):
    principal = request.user
    if principal is None or not getattr(principal, "is_authenticated", False):
        raise ApiError("not_authenticated", "Send your key as Authorization: Bearer <key>.", 401)
    return principal


def _building(request):
    principal = _principal(request)
    if principal.building is None:
        raise ApiError("building_key_required", "Only a building's own key can do this.", 403)
    return principal.building


def _scope(request, scope):
    principal = _principal(request)
    if not principal.allows(scope):
        raise ApiError("forbidden", f"This key does not have the {scope} scope.", 403)
    return principal


def _body(request):
    data = request.data
    if not isinstance(data, dict):
        raise ApiError("invalid", "The body must be a JSON object.")
    return data


def _text(data, key, required=False, limit=300):
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise ApiError("invalid", f"{key} is required.")
        return ""
    if not isinstance(value, (str, int, float)):
        raise ApiError("invalid", f"{key} must be text.")
    return str(value).strip()[:limit]


def _language(code):
    if not code:
        return None
    if not Language.objects.filter(code=code).exists():
        return None  # an unknown language falls back to the default rather than refusing the call
    return code


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None


# --------------------------------------------------------------------------- health

@api_view(["GET"])
@authentication_classes([])
def health(request):
    return Response({"ok": True})


# --------------------------------------------------------------------------- messages

def _send_answer(send_request, status=202):
    channels = [m.channel for m in send_request.messages.exclude(state="skipped").order_by("pk")]
    return Response({"id": send_request.public_id, "channels": channels}, status=status)


@endpoint(["POST"])
def messages(request):
    """POST /messages: send an app message from a template to one person."""
    from delivery.pipeline import send_app_message

    principal = _scope(request, "send")
    data = _body(request)
    key = _text(data, "idempotency_key", limit=200)
    if not key:
        raise ApiError("idempotency_key_required", "Send an idempotency_key so a repeated call cannot send twice.")
    template = Template.objects.filter(key=_text(data, "template", required=True, limit=120), kind="app").first()
    if template is None:
        raise ApiError("unknown_template", "No app message template has this key. Register it with PUT /templates.", 404)
    building = principal.building
    if building is not None and template.building_id not in (None, building.pk):
        raise ApiError("forbidden", "This template belongs to another building.", 403)
    owner = building or template.building

    existing = SendRequest.objects.filter(building=building, api_key=principal.api_key, idempotency_key=key).first()
    if existing is not None:
        return _send_answer(existing)  # the first answer again; nothing is sent

    to = data.get("to")
    if not isinstance(to, dict):
        raise ApiError("invalid", "to must be an object with an external_id or an email.")
    external_id, email = _text(to, "external_id", limit=120), _text(to, "email", limit=254).lower()
    if not external_id and not email:
        raise ApiError("invalid", "to needs an external_id or an email.")
    payload = data.get("data") or {}
    if not isinstance(payload, dict):
        raise ApiError("invalid", "data must be an object.")
    attributes = to.get("attributes") if isinstance(to.get("attributes"), dict) else None

    try:
        with transaction.atomic():
            person, _created = find_or_create_person(
                email=email or None, first_name=_text(to, "first_name", limit=120), last_name=_text(to, "last_name", limit=120),
                language_code=_language(_text(to, "language", limit=16)), source=owner.name if owner else "api",
                building=owner, external_id=external_id or None, attributes=attributes)
            send_request = SendRequest.objects.create(building=building, api_key=principal.api_key, template=template,
                                                      person=person, idempotency_key=key, data=payload)
    except IntegrityError:
        existing = SendRequest.objects.filter(building=building, api_key=principal.api_key, idempotency_key=key).first()
        if existing is None:
            raise ApiError("conflict", "This call clashed with another one. Send it again.", 409) from None
        return _send_answer(existing)
    send_app_message(template, person, payload, building=owner, request=send_request)
    return _send_answer(send_request)


@endpoint(["GET"])
def message_detail(request, message_id):
    """GET /messages/{id}: the state of one send, per channel."""
    principal = _scope(request, "send")
    send_request = SendRequest.get_by_public_id(message_id)
    if principal.building is not None and send_request.building_id != principal.building.pk:
        raise ApiError("not_found", "No send with this id.", 404)
    channels = []
    for m in send_request.messages.order_by("pk"):
        channels.append({
            "channel": m.channel, "state": m.state, "language": m.language, "used_fallback": m.used_fallback,
            "error": m.error if m.state in ("failed", "skipped") else "",
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        })
    return Response({"id": send_request.public_id, "template": send_request.template.key,
                     "to": send_request.person.public_id, "created_at": send_request.created_at.isoformat(),
                     "channels": channels})


# --------------------------------------------------------------------------- templates

@endpoint(["PUT"])
def templates(request):
    """PUT /templates: register or update the building's template definitions."""
    from content.services import register_templates

    building = _building(request)
    data = request.data
    definitions = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(definitions, list) or not all(isinstance(d, dict) for d in definitions):
        raise ApiError("invalid", 'Send a list of template definitions, or {"templates": [...]}.')
    for definition in definitions:
        if not definition.get("key") or not definition.get("name"):
            raise ApiError("invalid", "Every template needs a key and a name.")
    try:
        summary = register_templates(building, definitions)
    except ValueError as exc:
        raise ApiError("invalid", str(exc)) from exc
    return Response(summary if isinstance(summary, dict) else {"ok": True})


# --------------------------------------------------------------------------- lists

def _own_list(building, slug):
    lst = List.objects.filter(slug=slug).first()
    if lst is None:
        raise ApiError("unknown_list", "No list has this slug. Create it with PUT /lists/{slug}.", 404)
    if lst.building_id != building.pk:
        raise ApiError("forbidden", "This list does not belong to your building.", 403)
    return lst


def _translatable(value, fallback=None):
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v}
    if isinstance(value, str) and value.strip():
        return {Language.default_code(): value.strip()}
    return fallback if fallback is not None else {}


@endpoint(["PUT"])
def list_detail(request, slug):
    """PUT /lists/{slug}: create or update an app list or a building-owned campaign list."""
    building = _building(request)
    data = _body(request)
    if slug != slugify(slug) or len(slug) > 80:
        raise ApiError("invalid", "A list slug uses lower-case letters, digits and hyphens.")
    kind = data.get("kind")
    if kind not in (None, "app", "campaign"):
        raise ApiError("invalid", "kind is app or campaign.")
    lst = List.objects.filter(slug=slug).first()
    created = lst is None
    if created:
        name = _translatable(data.get("name"))
        if not name:
            raise ApiError("invalid", "name is required for a new list.")
        lst = List(slug=slug, kind=kind or "app", building=building, name=name, on_signup_form=False,
                   position=List.objects.count() + 1)
    else:
        if lst.building_id != building.pk:
            raise ApiError("forbidden", "This list does not belong to your building.", 403)
        if kind and kind != lst.kind:
            raise ApiError("invalid", "A list cannot change between app and campaign.")
        lst.name = _translatable(data.get("name"), lst.name)
    if "description" in data:
        lst.description = _translatable(data.get("description"))
    lst.save()
    return Response({"slug": lst.slug, "kind": lst.kind, "name": lst.name, "description": lst.description,
                     "members": lst.people().count()}, status=201 if created else 200)


def _consent_from(data, request):
    consent = data.get("consent")
    if not isinstance(consent, dict) or not str(consent.get("wording") or "").strip() or not consent.get("given_at"):
        raise ApiError("consent_required",
                       'This is a newsletter list. Send "consent": {"wording": "...", "given_at": "...", "ip": "..."} '
                       "with what the person agreed to.", 422)
    given_at = parse_datetime(str(consent["given_at"]))
    if given_at is None:
        raise ApiError("invalid", "consent.given_at must be a date and time such as 2026-01-31T18:40:00Z.")
    if timezone.is_naive(given_at):
        given_at = timezone.make_aware(given_at, datetime.timezone.utc)
    return {"wording": str(consent["wording"]).strip(), "given_at": given_at,
            "ip": str(consent.get("ip") or "").strip() or None,
            "user_agent": str(consent.get("user_agent") or "")}


def _member_person(building, external_id, data):
    email = _text(data, "email", limit=254).lower()
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else None
    person, _created = find_or_create_person(
        email=email or None, first_name=_text(data, "first_name", limit=120), last_name=_text(data, "last_name", limit=120),
        language_code=_language(_text(data, "language", limit=16)), source=building.name, building=building,
        external_id=external_id, attributes=attributes)
    return person


def _valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return value
    except (TypeError, ValueError):
        return None


@endpoint(["PUT", "DELETE"])
def list_member(request, slug, external_id):
    """PUT adds or updates a person on the list; DELETE removes them."""
    building = _building(request)
    lst = _own_list(building, slug)
    if request.method == "DELETE":
        person = find_person(building=building, external_id=external_id)
        if person is not None:
            subs = Subscription.objects.filter(person=person, list=lst)
            if lst.kind == "app":
                subs.delete()
            else:
                subs.exclude(state="off").update(state="off", updated_at=timezone.now())
        return Response(status=204)

    data = _body(request)
    wanted = data.get("channels")
    if wanted is not None and (not isinstance(wanted, list) or any(c not in ("email", "telegram", "push") for c in wanted)):
        raise ApiError("invalid", "channels is a list of email, telegram and push.")
    consent_data = _consent_from(data, request) if lst.kind == "campaign" else None
    with transaction.atomic():
        person = _member_person(building, external_id, data)
        channels = wanted
        if channels is None:
            channels = (["email"] if person.email else []) + ["telegram"]
        consent = None
        states = {}
        for channel in channels:
            sub = Subscription.objects.filter(person=person, list=lst, channel=channel).first()
            if sub is not None and sub.state == "on":
                states[channel] = "on"
                continue
            if sub is not None and sub.state == "off" and lst.kind == "app" and wanted is None:
                states[channel] = "off"  # they turned it off themselves; a sync does not turn it back on
                continue
            if lst.kind == "campaign" and consent is None:
                consent = record_consent(person, [lst], "api", consent_data["wording"], ip=_valid_ip(consent_data["ip"]),
                                         user_agent=consent_data["user_agent"], given_at=consent_data["given_at"],
                                         confirmed=True)
            try:
                states[channel] = set_subscription(person, lst, channel, True, consent=consent).state
            except ConsentRequired as exc:
                raise ApiError("consent_required", str(exc), 422) from exc
    return Response({"person": person.public_id, "list": lst.slug, "external_id": external_id, "channels": states})


# --------------------------------------------------------------------------- push members

@endpoint(["PUT", "DELETE"])
def push_member(request, audience, external_id):
    """PUT records that a person allowed push for this audience; DELETE that they turned it off."""
    building = _building(request)
    app = PushApp.objects.filter(building=building, audience=audience).select_related("list").first()
    if app is None:
        raise ApiError("unknown_audience", "No push audience with this name is connected for your building.", 404)
    if request.method == "DELETE":
        member = PushMember.objects.filter(push_app=app, external_id=external_id).select_related("person").first()
        if member is not None:
            person = member.person
            member.delete()
            if app.list_id and not PushMember.objects.filter(push_app__list=app.list, person=person).exists():
                set_subscription(person, app.list, "push", False)
        return Response(status=204)

    data = _body(request)
    with transaction.atomic():
        person = _member_person(building, external_id, data)
        PushMember.objects.update_or_create(push_app=app, external_id=external_id, defaults={"person": person})
        state = None
        if app.list_id:
            sub = Subscription.objects.filter(person=person, list=app.list, channel="push").first()
            if sub is None or sub.state != "on":
                given = data.get("consent") if isinstance(data.get("consent"), dict) else {}
                given_at = parse_datetime(str(given.get("given_at") or "")) if given.get("given_at") else None
                if given_at is not None and timezone.is_naive(given_at):
                    given_at = timezone.make_aware(given_at, datetime.timezone.utc)
                consent = record_consent(
                    person, [app.list], "api",
                    str(given.get("wording") or f"Allowed notifications for {app.label} in {building.name}"),
                    ip=_valid_ip(given.get("ip")), given_at=given_at, confirmed=True)
                sub = set_subscription(person, app.list, "push", True, consent=consent)
            state = sub.state
    return Response({"person": person.public_id, "audience": app.audience, "external_id": external_id, "push": state or "on"})


# --------------------------------------------------------------------------- subscribe

@endpoint(["POST"])
def subscribe(request):
    """POST /subscribe: the signup form for a custom website form."""
    from people.signup import process_signup

    _scope(request, "subscribe")
    data = _body(request)
    if data.get("consent") is not True:
        raise ApiError("consent_required", "consent must be true: the person has to tick the box themselves.", 422)
    wording = _text(data, "wording", limit=2000)
    if not wording:
        raise ApiError("wording_required", "Send the exact consent wording the person saw.", 422)
    email = _text(data, "email", required=True, limit=254).lower()
    lists = data.get("lists") or []
    if isinstance(lists, str):
        lists = [lists]
    if not isinstance(lists, list):
        raise ApiError("invalid", "lists is a list of list slugs.")
    link_params = data.get("link_params") if isinstance(data.get("link_params"), dict) else None
    try:
        process_signup(
            email=email, first_name=_text(data, "first_name", limit=120),
            language_code=_language(_text(data, "language", limit=16)), list_slugs=[str(s) for s in lists],
            telegram=bool(data.get("telegram")), wording=wording, ip=_valid_ip(data.get("ip")) or _client_ip(request),
            user_agent=_text(data, "user_agent", limit=1000) or request.META.get("HTTP_USER_AGENT", ""),
            link_params=link_params, method="api")
    except (ValueError, ConsentRequired) as exc:
        raise ApiError("invalid", str(exc)) from exc
    # The same answer whether or not the address was already known.
    return Response({"ok": True})


# --------------------------------------------------------------------------- people

@endpoint(["GET"])
def preferences_link(request, person_id):
    """GET /people/{id}/preferences-link: id is a Post Office id (p_12) or the building's own id."""
    building = _building(request)
    person = None
    pk = Person.pk_from_public_id(person_id)
    if pk is not None:
        # A building may only ask about people it knows.
        if PersonExternalId.objects.filter(building=building, person_id=pk).exists():
            person = Person.objects.filter(pk=pk).first()
    if person is None:
        person = find_person(building=building, external_id=person_id)
    if person is None:
        raise ApiError("unknown_person", "Your building has no person with this id.", 404)
    return Response({"person": person.public_id, "url": preferences_url(person)})


# --------------------------------------------------------------------------- late command replies

@endpoint(["POST"])
def command_reply(request, log_id):
    """POST /commands/{log_id}/reply: a late answer to a command, once, within 24 hours."""
    from commands.handler import late_reply

    log = CommandLog.get_by_public_id(log_id)
    token = request.query_params.get("token", "")
    if token:
        if not hmac.compare_digest(token.encode(), log.reply_token.encode()):
            raise ApiError("forbidden", "This reply address is not valid.", 403)
    else:
        _scope(request, "send")
    data = _body(request)
    text = data.get("text") if isinstance(data.get("text"), str) else data.get("reply")
    if not isinstance(text, str) or not text.strip():
        raise ApiError("invalid", "text is required.")
    if not late_reply(log, text.strip()):
        raise ApiError("too_late", "This command was already answered, or more than 24 hours have passed.", 409)
    return Response({"ok": True})
