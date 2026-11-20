import secrets

from django.db import models

from core.fields import EncryptedTextField
from core.models import PublicIdMixin, TimeStamped


def _secret():
    return secrets.token_urlsafe(24)


class EmailSender(TimeStamped, PublicIdMixin):
    """An email account Post Office sends from.

    Exactly one sender has purpose "app" (all app messages use it); any number
    have purpose "newsletter", one of them the default.
    """

    PUBLIC_PREFIX = "snd"
    PURPOSE_CHOICES = [("app", "App messages"), ("newsletter", "Newsletters")]
    KIND_CHOICES = [("gmail", "Gmail"), ("smtp", "Other mail server")]
    TLS_CHOICES = [("starttls", "STARTTLS"), ("ssl", "SSL/TLS"), ("none", "None")]
    STATE_CHOICES = [("ok", "Working"), ("error", "Not working"), ("untested", "Not tested")]

    purpose = models.CharField(max_length=12, choices=PURPOSE_CHOICES, default="newsletter")
    from_name = models.CharField(max_length=120)
    from_email = models.EmailField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="smtp")
    host = models.CharField(max_length=200, blank=True)
    port = models.PositiveIntegerField(default=587)
    tls_mode = models.CharField(max_length=10, choices=TLS_CHOICES, default="starttls")
    username = models.CharField(max_length=200, blank=True)
    password = EncryptedTextField(blank=True)
    daily_limit = models.PositiveIntegerField(null=True, blank=True)
    per_second = models.PositiveIntegerField(default=5)
    is_default = models.BooleanField(default=False)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default="untested")
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["purpose", "-is_default", "id"]

    def __str__(self):
        return f"{self.from_name} <{self.from_email}>"

    @classmethod
    def app_sender(cls):
        return cls.objects.filter(purpose="app").first()

    @classmethod
    def default_newsletter(cls):
        qs = cls.objects.filter(purpose="newsletter")
        return qs.filter(is_default=True).first() or qs.first()


class TelegramBot(TimeStamped, PublicIdMixin):
    PUBLIC_PREFIX = "bot"

    name = models.CharField(max_length=120)
    username = models.CharField(max_length=120, blank=True)  # without the @
    token = EncryptedTextField()
    is_default = models.BooleanField(default=False)
    webhook_secret = models.CharField(max_length=64, default=_secret)
    last_update_id = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["-is_default", "id"]

    def __str__(self):
        return f"@{self.username}" if self.username else self.name

    @classmethod
    def default(cls):
        return cls.objects.filter(is_default=True).first() or cls.objects.first()


class TelegramLink(TimeStamped):
    """A person connected to a bot."""

    person = models.ForeignKey("people.Person", on_delete=models.CASCADE, related_name="telegram_links")
    bot = models.ForeignKey(TelegramBot, on_delete=models.CASCADE, related_name="links")
    chat_id = models.BigIntegerField()
    username = models.CharField(max_length=120, blank=True)
    linked_at = models.DateTimeField(auto_now_add=True)
    blocked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("bot", "chat_id")]


class PushApp(TimeStamped, PublicIdMixin):
    """A building's push audience. Post Office asks the building to deliver; it never talks to phones."""

    PUBLIC_PREFIX = "push"

    building = models.ForeignKey("core.Building", on_delete=models.CASCADE, related_name="push_apps")
    audience = models.SlugField(max_length=80)  # e.g. offers-and-news
    label = models.CharField(max_length=120)  # e.g. "Offers and news"
    endpoint = models.URLField()
    secret = EncryptedTextField(default=_secret)
    list = models.ForeignKey("people.List", null=True, blank=True, on_delete=models.SET_NULL, related_name="push_apps")

    class Meta:
        unique_together = [("building", "audience")]

    def __str__(self):
        return f"{self.building.name}: {self.label}"


class PushMember(TimeStamped):
    """A person who allowed push for an audience. Kept in step by the building."""

    person = models.ForeignKey("people.Person", on_delete=models.CASCADE, related_name="push_memberships")
    push_app = models.ForeignKey(PushApp, on_delete=models.CASCADE, related_name="members")
    external_id = models.CharField(max_length=120)

    class Meta:
        unique_together = [("push_app", "external_id")]


class Webhook(TimeStamped, PublicIdMixin):
    """An extra output: a JSON copy of every campaign at send time. Not a channel people subscribe to."""

    PUBLIC_PREFIX = "wh"

    name = models.CharField(max_length=120)
    url = models.URLField()
    secret = EncryptedTextField(default=_secret)
    enabled = models.BooleanField(default=True)
    last_status = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return self.name
