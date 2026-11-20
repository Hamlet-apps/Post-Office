"""Fernet encryption for secrets at rest (SMTP passwords, bot tokens, header values)."""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_PREFIX = "fernet:"


def _fernet():
    return Fernet(settings.ENCRYPTION_KEY.encode())


def encrypt(value: str) -> str:
    if value in (None, ""):
        return ""
    return _PREFIX + _fernet().encrypt(str(value).encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def mask(value: str) -> str:
    """How a saved secret is shown in the UI: never the value itself."""
    return "••••••••" if value else ""


class EncryptedTextField(models.TextField):
    """Stores text encrypted; reads back plain text in Python."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value) if value else value

    def to_python(self, value):
        return decrypt(value) if isinstance(value, str) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value and not str(value).startswith(_PREFIX):
            return encrypt(value)
        return value
