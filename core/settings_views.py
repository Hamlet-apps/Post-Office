"""Settings (spec section 13): one page, six sections, each saved on its own. Owner only.

Sections: organisation, languages, connected buildings, consent, team, API keys.
Every action is a plain POST that returns to the section it came from with a
toast. A new building key or API key is rendered once in the response to the
POST that made it and is never stored (only its hash is).
"""
import secrets
import zoneinfo

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from .models import KNOWN_LANGUAGES, ApiKey, Building, Language, Organization, TeamMember
from .permissions import role_required
from .views import COMMON_TIMEZONES

SECTIONS = [
    ("st-org", gettext_lazy("Organisation")),
    ("st-lang", gettext_lazy("Languages")),
    ("st-bld", gettext_lazy("Connected buildings")),
    ("st-cons", gettext_lazy("Consent")),
    ("st-team", gettext_lazy("Team")),
    ("st-keys", gettext_lazy("API keys")),
]
ROLE_LABELS = [
    ("owner", gettext_lazy("Owner: everything")),
    ("editor", gettext_lazy("Editor: write and send")),
    ("viewer", gettext_lazy("Viewer: reports only")),
]
SCOPE_LABELS = [
    ("send", gettext_lazy("Send messages only")),
    ("subscribe", gettext_lazy("Add subscribers only")),
    ("all", gettext_lazy("Send messages and add subscribers")),
]
MEMBER_PREFIX = "tm_"


# --------------------------------------------------------------------------- the page

def _back(section):
    return redirect(f"{reverse('settings')}#{section}")


def invite_url(member):
    return f"{settings.PUBLIC_URL}/invite/{member.invite_token}/"


def _context(request, **extra):
    org = Organization.get()
    languages = list(Language.objects.annotate(people_count=Count("people")))
    present = {lang.code for lang in languages}
    default_language = next((lang for lang in languages if lang.is_default), languages[0] if languages else None)

    buildings = list(Building.objects.all())
    try:  # what each building sends: its app message templates and the app lists it keeps
        from content.models import Template
        from people.models import List

        default_code = default_language.code if default_language else "en"
        for building in buildings:
            names = list(Template.objects.filter(building=building).values_list("name", flat=True)[:6])
            names += [lst.display_name(default_code) for lst in List.objects.filter(building=building)[:4]]
            building.sends = names
    except Exception:  # those apps are built in parallel; the page must still open
        for building in buildings:
            building.sends = []
    for building in buildings:
        building.connected = bool(building.key_hash)
        building.host = building.base_url.split("://", 1)[-1].rstrip("/")

    members = list(TeamMember.objects.filter(is_active=True).order_by("created_at", "pk"))
    owners = _joined_owner_count()
    for member in members:
        member.settings_id = f"{MEMBER_PREFIX}{member.pk}"
        member.is_you = member.pk == request.user.pk
        member.pending = bool(member.invite_token)
        member.invite_link = invite_url(member) if member.pending else ""
        member.last_owner = member.role == "owner" and not member.pending and owners <= 1

    timezones = list(COMMON_TIMEZONES)
    if org.timezone and org.timezone not in timezones:
        timezones.insert(0, org.timezone)

    ctx = {
        "org": org,
        "sections": SECTIONS,
        "timezones": timezones,
        "languages": languages,
        "default_language": default_language,
        "addable_languages": [{"code": c, "name": n, "english": e} for c, n, e in KNOWN_LANGUAGES if c not in present],
        "unshipped_languages": [lang for lang in languages if not lang.has_builtin_wording],
        "buildings": buildings,
        "building_kinds": Building.KIND_CHOICES,
        "members": members,
        "role_labels": ROLE_LABELS,
        "api_keys": ApiKey.objects.filter(revoked_at__isnull=True),
        "revoked_keys": ApiKey.objects.filter(revoked_at__isnull=False)[:5],
        "scope_labels": SCOPE_LABELS,
        "form": {},
        "errors": {},
    }
    ctx.update(extra)
    return ctx


def _render(request, status=200, **extra):
    return render(request, "core/settings/index.html", _context(request, **extra), status=status)


@role_required("owner")
def settings_index(request):
    return _render(request)


# --------------------------------------------------------------------------- organisation

@role_required("owner")
@require_POST
def organisation(request):
    org = Organization.get()
    data = {k: request.POST.get(k, "").strip() for k in ("name", "reply_to", "mailing_address", "timezone")}
    errors = {}
    if not data["name"]:
        errors["name"] = _("Enter the name of your organisation.")
    if data["reply_to"]:
        try:
            validate_email(data["reply_to"])
        except ValidationError:
            errors["reply_to"] = _("Enter a working email address.")
    try:
        zoneinfo.ZoneInfo(data["timezone"])
    except Exception:
        errors["timezone"] = _("Choose a time zone.")
    if errors:
        return _render(request, status=422, form={"org": data}, errors={"org": errors}, focus="st-org")
    org.name = data["name"]
    org.reply_to = data["reply_to"]
    org.mailing_address = data["mailing_address"]
    org.timezone = data["timezone"]
    org.save()
    messages.success(request, _("Organisation details are saved."))
    return _back("st-org")


# --------------------------------------------------------------------------- languages

@role_required("owner")
@require_POST
def language_add(request):
    code = request.POST.get("code", "")
    known = {c: (n, e) for c, n, e in KNOWN_LANGUAGES}
    if code not in known:
        messages.error(request, _("Choose a language from the list."))
    elif Language.objects.filter(code=code).exists():
        messages.error(request, _("That language is already on the list."))
    else:
        name, english = known[code]
        position = (Language.objects.order_by("-position").values_list("position", flat=True).first() or 0) + 1
        lang = Language.objects.create(code=code, name=name, english_name=english, position=position,
                                       is_default=not Language.objects.exists())
        if lang.has_builtin_wording:
            messages.success(request, _("%(language)s is added. Every campaign and template now has a tab for it.") % {"language": name})
        else:
            messages.success(request, _("%(language)s is added. Its built-in wording is not translated yet, so that part shows in English.")
                             % {"language": name})
    return _back("st-lang")


def _language_or_404(code):
    lang = Language.objects.filter(code=code).first()
    if lang is None:
        raise Http404("No such language")
    return lang


@role_required("owner")
@require_POST
def language_default(request, code):
    lang = _language_or_404(code)
    Language.objects.exclude(pk=lang.pk).filter(is_default=True).update(is_default=False)
    Language.objects.filter(pk=lang.pk).update(is_default=True)
    messages.success(request, _("%(language)s is now the default language.") % {"language": lang.name})
    return _back("st-lang")


@role_required("owner")
@require_POST
def language_remove(request, code):
    """Remove a language. Its people move to the default; the default itself cannot go."""
    lang = _language_or_404(code)
    default = Language.default()
    if lang.is_default or default is None or default.pk == lang.pk:
        messages.error(request, _("The default language cannot be removed. Make another language the default first."))
        return _back("st-lang")
    moved = lang.people.update(language=default)
    name = lang.name
    lang.delete()
    if moved:
        messages.success(request, _("%(language)s is removed. %(count)s people now get %(default)s.")
                         % {"language": name, "count": moved, "default": default.name})
    else:
        messages.success(request, _("%(language)s is removed.") % {"language": name})
    return _back("st-lang")


@role_required("owner")
@require_POST
def language_missing(request):
    org = Organization.get()
    choice = request.POST.get("missing_translation")
    if choice in dict(Organization.MISSING_CHOICES):
        org.missing_translation = choice
        org.save(update_fields=["missing_translation", "updated_at"])
        messages.success(request, _("Saved."))
    return _back("st-lang")


# --------------------------------------------------------------------------- connected buildings

@role_required("owner")
@require_POST
def building_connect(request):
    name = request.POST.get("name", "").strip()
    kind = request.POST.get("kind", "other")
    base_url = request.POST.get("base_url", "").strip()
    if base_url and "://" not in base_url:
        base_url = "https://" + base_url
    if not name:
        return _render(request, status=422, focus="st-bld", errors={"bld": {"name": _("Give the building a name.")}},
                       form={"bld": {"name": name, "kind": kind, "base_url": base_url}})
    building = Building.objects.create(name=name[:120], kind=kind if kind in dict(Building.KIND_CHOICES) else "other",
                                       base_url=base_url[:200])
    return _render(request, focus="st-bld", new_key={"owner": "building", "name": building.name, "key": building.issue_key()})


@role_required("owner")
@require_POST
def building_rotate(request, building_id):
    """Issue a new key. The old one stops working at once."""
    building = Building.get_by_public_id(building_id)
    return _render(request, focus="st-bld",
                   new_key={"owner": "building", "name": building.name, "key": building.issue_key(), "rotated": True})


@role_required("owner")
@require_POST
def building_disconnect(request, building_id):
    """Take the building's key away. Its templates and history stay; it can be connected again."""
    building = Building.get_by_public_id(building_id)
    building.key_hash = ""
    building.key_prefix = ""
    building.save(update_fields=["key_hash", "key_prefix", "updated_at"])
    messages.success(request, _("%(name)s is disconnected. It can no longer send through Post Office.") % {"name": building.name})
    return _back("st-bld")


@role_required("owner")
@require_POST
def building_remove(request, building_id):
    """Remove a disconnected building for good, with the templates it registered."""
    building = Building.get_by_public_id(building_id)
    if building.key_hash:
        messages.error(request, _("Disconnect %(name)s first.") % {"name": building.name})
        return _back("st-bld")
    name = building.name
    building.delete()
    messages.success(request, _("%(name)s is removed.") % {"name": name})
    return _back("st-bld")


# --------------------------------------------------------------------------- consent

@role_required("owner")
@require_POST
def consent(request):
    org = Organization.get()
    org.confirm_by_email = "confirm_by_email" in request.POST
    org.keep_consent_proof = "keep_consent_proof" in request.POST
    org.tracking_enabled = "tracking_enabled" in request.POST
    # The unsubscribe link is always on; there is nothing to save for it.
    org.save(update_fields=["confirm_by_email", "keep_consent_proof", "tracking_enabled", "updated_at"])
    messages.success(request, _("Consent settings are saved."))
    return _back("st-cons")


# --------------------------------------------------------------------------- team

def _joined_owner_count():
    """Owners who can actually sign in (an invited owner who has not joined yet does not count)."""
    return TeamMember.objects.filter(role="owner", is_active=True, invite_token="").count()


def _member_or_404(member_id):
    tail = member_id[len(MEMBER_PREFIX):] if member_id.startswith(MEMBER_PREFIX) else ""
    member = TeamMember.objects.filter(pk=int(tail), is_active=True).first() if tail.isdigit() else None
    if member is None:
        raise Http404("No such team member")
    return member


def _is_last_owner(member):
    return member.role == "owner" and not member.invite_token and _joined_owner_count() <= 1


def _email_invite(request, member):
    """Email the invite link. Returns True only when the email was handed over for sending."""
    try:
        from delivery.pipeline import send_system_email
    except Exception:  # the sending pipeline is not there: the link can still be copied
        return False
    org = Organization.get()
    link = invite_url(member)
    inviter = request.user.name or request.user.email
    subject = _("You are invited to %(org)s on Post Office") % {"org": org.name or "Post Office"}
    intro = _("%(inviter)s invited you to join %(org)s on Post Office, where the team writes and sends its messages.") % {
        "inviter": inviter, "org": org.name or "Post Office"}
    action = _("Choose your password")
    outro = _("If you were not expecting this, you can ignore this email.")
    html = (
        f"<p>{escape(intro)}</p>"
        f'<p><a href="{escape(link)}">{escape(action)}</a></p>'
        f"<p>{escape(outro)}</p>"
    )
    text = f"{intro}\n\n{action}: {link}\n\n{outro}"
    try:
        return send_system_email(member.email, subject, html, text=text, person=None) is not None
    except Exception:
        return False


@role_required("owner")
@require_POST
def team_invite(request):
    email = request.POST.get("email", "").strip().lower()
    role = request.POST.get("role", "editor")
    form = {"team": {"email": email, "role": role}}
    error = ""
    try:
        validate_email(email)
    except ValidationError:
        error = _("Enter a working email address.")
    if role not in dict(TeamMember.ROLE_CHOICES):
        role = "editor"
    existing = TeamMember.objects.filter(email=email).first() if not error else None
    if existing is not None and existing.is_active:
        error = _("%(email)s is already on the team.") % {"email": email}
    if error:
        return _render(request, status=422, focus="st-team", form=form, errors={"team": {"email": error}})
    token = secrets.token_urlsafe(32)
    if existing is not None:  # someone removed earlier under an older scheme: invite them afresh
        existing.role, existing.is_active, existing.invite_token, existing.invited_at = role, True, token, timezone.now()
        existing.set_unusable_password()
        existing.save()
        member = existing
    else:
        member = TeamMember.objects.create_user(email, None, role=role, invite_token=token, invited_at=timezone.now())
    emailed = _email_invite(request, member)
    return _render(request, focus="st-team", new_invite={"email": member.email, "link": invite_url(member), "emailed": emailed})


@role_required("owner")
@require_POST
def team_role(request, member_id):
    member = _member_or_404(member_id)
    role = request.POST.get("role", "")
    if role not in dict(TeamMember.ROLE_CHOICES):
        messages.error(request, _("Choose a role from the list."))
    elif role != "owner" and _is_last_owner(member):
        if member.pk == request.user.pk:
            messages.error(request, _("You are the only owner. Make someone else an owner before you change your own role."))
        else:
            messages.error(request, _("There must always be at least one owner."))
    elif role != member.role:
        member.role = role
        member.save(update_fields=["role", "updated_at"])
        messages.success(request, _("%(name)s is now %(role)s.") % {"name": member, "role": member.get_role_display().lower()})
    return _back("st-team")


@role_required("owner")
@require_POST
def team_remove(request, member_id):
    member = _member_or_404(member_id)
    if _is_last_owner(member):
        if member.pk == request.user.pk:
            messages.error(request, _("You are the only owner, so you cannot remove yourself. Make someone else an owner first."))
        else:
            messages.error(request, _("There must always be at least one owner."))
        return _back("st-team")
    name = str(member)
    removing_self = member.pk == request.user.pk
    member.delete()
    if removing_self:
        return redirect("sign_in")
    messages.success(request, _("%(name)s can no longer sign in.") % {"name": name})
    return _back("st-team")


# --------------------------------------------------------------------------- API keys

@role_required("owner")
@require_POST
def key_create(request):
    name = request.POST.get("name", "").strip()
    scope = request.POST.get("scope", "send")
    if scope not in dict(ApiKey.SCOPE_CHOICES):
        scope = "send"
    if not name:
        return _render(request, status=422, focus="st-keys", form={"key": {"name": name, "scope": scope}},
                       errors={"key": {"name": _("Say what the key is for, so you can recognise it later.")}})
    api_key, full_key = ApiKey.issue(name[:120], scope)
    return _render(request, focus="st-keys", new_key={"owner": "api", "name": api_key.name, "key": full_key})


@role_required("owner")
@require_POST
def key_revoke(request, key_id):
    api_key = ApiKey.get_by_public_id(key_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=["revoked_at", "updated_at"])
    messages.success(request, _("The key for %(name)s no longer works.") % {"name": api_key.name})
    return _back("st-keys")


urlpatterns = [
    path("settings/", settings_index, name="settings"),
    path("settings/organisation/", organisation, name="settings_organisation"),
    path("settings/languages/add/", language_add, name="settings_language_add"),
    path("settings/languages/missing/", language_missing, name="settings_language_missing"),
    path("settings/languages/<str:code>/default/", language_default, name="settings_language_default"),
    path("settings/languages/<str:code>/remove/", language_remove, name="settings_language_remove"),
    path("settings/buildings/connect/", building_connect, name="settings_building_connect"),
    path("settings/buildings/<str:building_id>/rotate/", building_rotate, name="settings_building_rotate"),
    path("settings/buildings/<str:building_id>/disconnect/", building_disconnect, name="settings_building_disconnect"),
    path("settings/buildings/<str:building_id>/remove/", building_remove, name="settings_building_remove"),
    path("settings/consent/", consent, name="settings_consent"),
    path("settings/team/invite/", team_invite, name="settings_team_invite"),
    path("settings/team/<str:member_id>/role/", team_role, name="settings_team_role"),
    path("settings/team/<str:member_id>/remove/", team_remove, name="settings_team_remove"),
    path("settings/keys/create/", key_create, name="settings_key_create"),
    path("settings/keys/<str:key_id>/revoke/", key_revoke, name="settings_key_revoke"),
]
