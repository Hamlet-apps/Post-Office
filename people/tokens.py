"""Signed links that name a person: preferences page, email confirmation, Telegram connect."""
import secrets

from django.conf import settings
from django.core import signing
from django.core.cache import cache

from .models import Person

_PREFS_SALT = "po.preferences"
_CONFIRM_SALT = "po.confirm"
_CONNECT_SALT = "po.telegram-connect"
CONNECT_MAX_AGE = 60 * 60 * 24 * 30


def preferences_token(person):
    return signing.dumps(person.pk, salt=_PREFS_SALT, compress=True)


def person_from_preferences_token(token):
    try:
        pk = signing.loads(token, salt=_PREFS_SALT)
    except signing.BadSignature:
        return None
    return Person.objects.filter(pk=pk).first()


def preferences_url(person):
    return f"{settings.PUBLIC_URL}/p/{preferences_token(person)}"


def confirm_token(consent):
    return signing.dumps(consent.pk, salt=_CONFIRM_SALT)


def consent_pk_from_confirm_token(token, max_age=60 * 60 * 24 * 14):
    try:
        return signing.loads(token, salt=_CONFIRM_SALT, max_age=max_age)
    except signing.BadSignature:
        return None


def connect_token(person):
    """A signed, single-use value for https://t.me/<bot>?start=<token>.

    Telegram allows 64 characters of A-Z a-z 0-9 _ - in a start parameter, so the
    token is a short random key; what it names is kept in the cache.
    """
    key = secrets.token_urlsafe(24)
    cache.set(f"tgconnect:{key}", person.pk, CONNECT_MAX_AGE)
    return key


def person_from_connect_token(token, consume=True):
    key = f"tgconnect:{token}"
    pk = cache.get(key)
    if pk is None:
        return None
    if consume:
        cache.delete(key)
    return Person.objects.filter(pk=pk).first()


def connect_url(person, bot):
    return f"https://t.me/{bot.username}?start={connect_token(person)}"
