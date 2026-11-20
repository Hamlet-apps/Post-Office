import hashlib

from django.db import models

from core.models import Language, PublicIdMixin, TimeStamped, tr

CHANNELS = ["email", "telegram", "push"]
CHANNEL_CHOICES = [("email", "Email"), ("telegram", "Telegram"), ("push", "Push")]


def normalize_email(email):
    return (email or "").strip().lower() or None


def hash_email(email):
    return hashlib.sha256((normalize_email(email) or "").encode()).hexdigest()


class Person(TimeStamped, PublicIdMixin):
    """Someone who can receive messages."""

    PUBLIC_PREFIX = "p"
    EMAIL_STATES = [("ok", "OK"), ("bounced", "Bounced"), ("complained", "Complained")]

    # Always stored lower-case (see save), which makes the unique index case-insensitive.
    email = models.EmailField(null=True, blank=True, unique=True)
    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    language = models.ForeignKey(Language, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    attributes = models.JSONField(default=dict, blank=True)
    email_state = models.CharField(max_length=12, choices=EMAIL_STATES, default="ok")
    source = models.CharField(max_length=40, blank=True)  # signup_form, import, api, staff, a building's name

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.full_name or self.email or self.public_id

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        if self.language_id is None:
            self.language = Language.default()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name(self):
        return self.full_name or self.email or self.public_id

    @property
    def language_code(self):
        return self.language.code if self.language_id else Language.default_code()

    @property
    def email_reachable(self):
        return bool(self.email) and self.email_state == "ok"


class PersonExternalId(TimeStamped):
    """The id a building knows a person by (cust_511, vol_88)."""

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="external_ids")
    building = models.ForeignKey("core.Building", on_delete=models.CASCADE, related_name="external_ids")
    external_id = models.CharField(max_length=120)

    class Meta:
        unique_together = [("building", "external_id")]


class List(TimeStamped, PublicIdMixin):
    """A campaign list (people choose to join) or an app list (a building keeps it in step)."""

    PUBLIC_PREFIX = "lst"
    KIND_CHOICES = [("campaign", "Campaign list"), ("app", "App list")]

    name = models.JSONField(default=dict)  # translatable: {"en": "Newsletter", "uk": "..."}
    slug = models.SlugField(max_length=80, unique=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="campaign")
    building = models.ForeignKey("core.Building", null=True, blank=True, on_delete=models.SET_NULL, related_name="lists")
    on_signup_form = models.BooleanField(default=False)
    description = models.JSONField(default=dict, blank=True)  # translatable
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.display_name()

    def display_name(self, code=None):
        return tr(self.name, code or Language.default_code()) or self.slug

    def display_description(self, code=None):
        return tr(self.description, code or Language.default_code())

    @property
    def is_app(self):
        return self.kind == "app"

    @property
    def message_type(self):
        return "service" if self.kind == "app" else "newsletter"

    def people(self):
        """People with at least one subscription that is on."""
        return Person.objects.filter(subscriptions__list=self, subscriptions__state="on").distinct()


class ConsentRecord(TimeStamped):
    """Proof that a person agreed to receive newsletters (CASL)."""

    METHOD_CHOICES = [("signup_form", "Signup form"), ("import", "Import"), ("api", "API"), ("staff", "Staff")]

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="consents")
    lists = models.ManyToManyField(List, related_name="consents", blank=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    wording = models.TextField(blank=True)  # the exact text shown
    language = models.CharField(max_length=16, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    link_params = models.JSONField(default=dict, blank=True)  # pre-filled link parameters, if any
    note = models.TextField(blank=True)  # required for method staff: how consent was given
    recorded_by = models.ForeignKey("core.TeamMember", null=True, blank=True, on_delete=models.SET_NULL)
    given_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-given_at"]


class Subscription(TimeStamped):
    """One person, one list, one channel."""

    STATE_CHOICES = [("unconfirmed", "Waiting for confirmation"), ("on", "On"), ("off", "Off")]

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="subscriptions")
    list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="subscriptions")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default="on")
    consent = models.ForeignKey(ConsentRecord, null=True, blank=True, on_delete=models.SET_NULL, related_name="subscriptions")

    class Meta:
        unique_together = [("person", "list", "channel")]
        indexes = [models.Index(fields=["list", "channel", "state"])]


class Segment(TimeStamped):
    """A saved filter over people. Listed only in version 1; there is no builder."""

    name = models.CharField(max_length=120)
    definition = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name


class Suppression(TimeStamped):
    """Hashed emails of deleted people, so an import cannot silently resubscribe them."""

    email_hash = models.CharField(max_length=64, unique=True)

    @classmethod
    def is_suppressed(cls, email):
        return cls.objects.filter(email_hash=hash_email(email)).exists()
