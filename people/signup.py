"""Signing a person up for campaign lists (spec sections 8 and 11).

The public form at /join and the REST API's POST /subscribe both call
process_signup(); nothing else writes a signup.

    result = process_signup(email="olena.k@example.com", first_name="Olena", language_code="uk",
                            list_slugs=["newsletter"], wording="I agree to receive these messages...",
                            ip="203.0.113.9", user_agent="...", link_params={})

Rules kept here:
- The caller passes the exact consent wording the person saw; it is stored as proof.
- An address that already exists is never changed and never revealed: the caller gets
  the same kind of result and the person gets an email with a link to their preferences.
- With Organization.confirm_by_email on, new email subscriptions start unconfirmed and
  one confirmation email is sent.
"""
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext as _

from core.models import Language, Organization

from . import services
from .models import ConsentRecord, List, Person, normalize_email
from .tokens import confirm_token, preferences_url

log = logging.getLogger(__name__)

RATE_LIMIT = 10  # signups per IP address ...
RATE_WINDOW = 60 * 60  # ... per hour


@dataclass
class SignupResult:
    person: Person
    existing: bool
    consent: ConsentRecord | None
    lists: list = field(default_factory=list)
    confirmation_sent: bool = False


def wording_language(code):
    """The catalogue to use for built-in wording: the person's language when it ships, else English."""
    base = (code or "").split("-")[0]
    return base if base in settings.SHIPPED_TRANSLATIONS else "en"


def consent_wording(code, prefilled=False):
    """The consent sentence shown on the signup form, in the reader's language.

    On the normal form it sits beside a tick box. On a pre-filled link there is no
    tick box: the sentence sits beside the Subscribe button, and pressing it is the consent.
    """
    with translation.override(wording_language(code)):
        if prefilled:
            return _("By pressing Subscribe, I agree to receive these messages. I can unsubscribe at any time.")
        return _("I agree to receive these messages. I can unsubscribe at any time.")


def too_many_signups(ip, limit=RATE_LIMIT, window=RATE_WINDOW):
    """Count one attempt for this IP address; true when it is over the limit."""
    if not ip:
        return False
    key = f"signup-rate:{ip}"
    if cache.add(key, 1, window):
        return False
    try:
        count = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, window)
        return False
    return count > limit


def process_signup(*, email, first_name="", language_code=None, list_slugs=(), telegram=False, wording,
                   ip=None, user_agent="", link_params=None, method="signup_form") -> SignupResult:
    """Create the person, their subscriptions and the proof of consent.

    list_slugs names campaign lists (app lists and unknown slugs are ignored).
    telegram also switches the same lists on for Telegram; the person still has to
    connect a bot before anything arrives there.
    """
    email = normalize_email(email)
    if not email:
        raise ValueError("An email address is needed to sign up.")
    slugs = [s for s in (list_slugs or []) if s]
    lists = list(List.objects.filter(kind="campaign", slug__in=slugs))

    existing = Person.objects.filter(email=email).first()
    if existing is not None:
        # Say nothing to the caller; tell the owner of the address instead.
        send_preferences_email(existing)
        return SignupResult(person=existing, existing=True, consent=None, lists=lists)

    if language_code and not Language.objects.filter(code=language_code).exists():
        language_code = None
    with transaction.atomic():
        person, _created = services.find_or_create_person(
            email=email, first_name=(first_name or "").strip()[:120], language_code=language_code, source=method)
        consent = None
        unconfirmed = services.needs_email_confirmation()
        if lists:
            consent = services.record_consent(
                person, lists, method, wording, language=language_code or person.language_code,
                ip=ip, user_agent=user_agent, link_params=link_params or {})
            for lst in lists:
                services.set_subscription(person, lst, "email", True, consent=consent, unconfirmed=unconfirmed)
                if telegram:
                    services.set_subscription(person, lst, "telegram", True, consent=consent)
    sent = False
    if consent is not None and unconfirmed:
        sent = send_confirmation_email(person, consent)
    return SignupResult(person=person, existing=False, consent=consent, lists=lists, confirmation_sent=sent)


# --------------------------------------------------------------------------- the two emails

def _send(person, subject, blocks, text, lang):
    """Send one email from the app sender. False when nothing could be sent."""
    try:
        from content.render import build_context, render_blocks, wrap_email
        from delivery.pipeline import send_system_email

        org = Organization.get()
        context = build_context(person, {}, {"preferences": preferences_url(person)})
        html = wrap_email(render_blocks(blocks, context), org=org, lang=lang, preview_text=subject)
        return send_system_email(person.email, subject, html, text, person=person) is not None
    except Exception:  # a failed email must not change what the public page shows
        log.exception("Could not send a signup email")
        return False


def send_confirmation_email(person, consent):
    """The one email with a confirm button."""
    url = f"{settings.PUBLIC_URL}/confirm/{confirm_token(consent)}"
    org = Organization.get()
    code = consent.language or person.language_code
    with translation.override(wording_language(code)):
        names = ", ".join(lst.display_name(code) for lst in consent.lists.all())
        subject = _("Please confirm your email address")
        greeting = _("Hi %(name)s,") % {"name": person.first_name} if person.first_name else _("Hi,")
        body = _("You asked to hear from %(org)s about: %(lists)s. Press the button to confirm this is your email address.") % {
            "org": org.name, "lists": names}
        ignore = _("If this was not you, ignore this email and you will hear nothing more.")
        button = _("Confirm my email address")
    blocks = [
        {"id": "h", "type": "heading", "text": _escape(subject)},
        {"id": "t", "type": "text", "text": _escape(f"{greeting}\n\n{body}")},
        {"id": "b", "type": "button", "text": _escape(button), "href": url},
        {"id": "i", "type": "text", "text": _escape(ignore)},
    ]
    text = f"{greeting}\n\n{body}\n\n{button}: {url}\n\n{ignore}\n\n{org.name}"
    return _send(person, subject, blocks, text, code)


def send_preferences_email(person):
    """Sent when someone submits the signup form with an address we already have."""
    url = preferences_url(person)
    org = Organization.get()
    code = person.language_code
    with translation.override(wording_language(code)):
        subject = _("Your choices at %(org)s") % {"org": org.name}
        greeting = _("Hi %(name)s,") % {"name": person.first_name} if person.first_name else _("Hi,")
        body = _("Someone, probably you, just filled in our signup form with this email address. "
                 "You are already with us, so nothing was changed. "
                 "Press the button to see and change what you hear about.")
        ignore = _("If this was not you, there is nothing you need to do.")
        button = _("Choose what I hear about")
    blocks = [
        {"id": "h", "type": "heading", "text": _escape(subject)},
        {"id": "t", "type": "text", "text": _escape(f"{greeting}\n\n{body}")},
        {"id": "b", "type": "button", "text": _escape(button), "href": url},
        {"id": "i", "type": "text", "text": _escape(ignore)},
    ]
    text = f"{greeting}\n\n{body}\n\n{button}: {url}\n\n{ignore}\n\n{org.name}"
    return _send(person, subject, blocks, text, code)


def _escape(text):
    """Block text treats {name} as a placeholder and [label](url) as a link.

    A first name typed into the public form ends up in these emails, so neither may
    survive: otherwise anyone could put their own link into an email we send.
    """
    return (text or "").translate(str.maketrans({"{": "(", "}": ")", "[": "(", "]": ")"}))
