import hashlib
import secrets
import zoneinfo

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.http import Http404
from django.utils import timezone


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublicIdMixin:
    """Short prefixed ids for URLs and the API (p_2041, cmp_88), never raw keys."""

    PUBLIC_PREFIX = "x"

    @property
    def public_id(self):
        return f"{self.PUBLIC_PREFIX}_{self.pk}"

    @classmethod
    def pk_from_public_id(cls, public_id):
        prefix = cls.PUBLIC_PREFIX + "_"
        if not isinstance(public_id, str) or not public_id.startswith(prefix):
            return None
        tail = public_id[len(prefix):]
        return int(tail) if tail.isdigit() else None

    @classmethod
    def get_by_public_id(cls, public_id, queryset=None):
        """Return the row or raise Http404."""
        pk = cls.pk_from_public_id(public_id)
        qs = queryset if queryset is not None else cls._default_manager.all()
        obj = qs.filter(pk=pk).first() if pk is not None else None
        if obj is None:
            raise Http404(f"No {cls.__name__} {public_id}")
        return obj


def tr(value, code, default_code=None):
    """Read a translatable JSON value ({"en": "...", "uk": "..."}) for a language."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value:
        return ""
    if value.get(code):
        return value[code]
    if default_code is None:
        default_code = Language.default_code()
    if value.get(default_code):
        return value[default_code]
    return next((v for v in value.values() if v), "")


class Organization(TimeStamped):
    """The one organization this install serves. Use Organization.get()."""

    MISSING_CHOICES = [("fallback", "Send the default language"), ("hold", "Hold the campaign")]

    name = models.CharField(max_length=200, blank=True)
    reply_to = models.EmailField(blank=True)
    mailing_address = models.TextField(blank=True)
    website = models.URLField(blank=True)
    timezone = models.CharField(max_length=64, default="America/Vancouver")
    missing_translation = models.CharField(max_length=10, choices=MISSING_CHOICES, default="fallback")
    confirm_by_email = models.BooleanField(default=True)
    keep_consent_proof = models.BooleanField(default=True)
    tracking_enabled = models.BooleanField(default=True)
    # TODO(gatehouse): the "Sign in with Gatehouse" switch in Settings stores this,
    # but nothing reads it until Gatehouse sign-in is built.
    gatehouse_enabled = models.BooleanField(default=False)
    # Signup form options: show_language, show_telegram, show_first_name, telegram_in_footer.
    signup_form = models.JSONField(default=dict, blank=True)
    setup_complete = models.BooleanField(default=False)

    def __str__(self):
        return self.name or "Organization"

    @classmethod
    def get(cls):
        org = cls.objects.order_by("pk").first()
        if org is None:
            org = cls.objects.create()
        return org

    @property
    def tz(self):
        try:
            return zoneinfo.ZoneInfo(self.timezone)
        except Exception:
            return zoneinfo.ZoneInfo("UTC")

    def local_now(self):
        return timezone.now().astimezone(self.tz)

    def signup_option(self, key, default=True):
        return (self.signup_form or {}).get(key, default)


# Languages offered in setup and Settings: code, native name, English name.
KNOWN_LANGUAGES = [
    ("en", "English", "English"),
    ("uk", "Українська", "Ukrainian"),
    ("fr", "Français", "French"),
    ("es", "Español", "Spanish"),
    ("pl", "Polski", "Polish"),
    ("zh", "中文", "Chinese"),
    ("pa", "ਪੰਜਾਬੀ", "Punjabi"),
    ("tl", "Tagalog", "Tagalog"),
    ("de", "Deutsch", "German"),
    ("pt", "Português", "Portuguese"),
    ("it", "Italiano", "Italian"),
    ("ar", "العربية", "Arabic"),
    ("hi", "हिन्दी", "Hindi"),
    ("vi", "Tiếng Việt", "Vietnamese"),
    ("ko", "한국어", "Korean"),
    ("ja", "日本語", "Japanese"),
    ("ru", "Русский", "Russian"),
    ("fa", "فارسی", "Persian"),
]


class Language(TimeStamped):
    code = models.CharField(max_length=16, unique=True)  # BCP 47
    name = models.CharField(max_length=80)  # native name, shown to readers
    english_name = models.CharField(max_length=80, blank=True)
    is_default = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name

    @classmethod
    def default(cls):
        return cls.objects.filter(is_default=True).first() or cls.objects.first()

    @classmethod
    def default_code(cls):
        lang = cls.default()
        return lang.code if lang else "en"

    @classmethod
    def codes(cls):
        return list(cls.objects.values_list("code", flat=True))

    @property
    def has_builtin_wording(self):
        from django.conf import settings

        return self.code.split("-")[0] in settings.SHIPPED_TRANSLATIONS


class TeamMemberManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("role", "owner")
        return self.create_user(email, password, **extra)


class TeamMember(AbstractBaseUser, TimeStamped):
    """Someone who signs in to Post Office. Separate from people."""

    ROLE_CHOICES = [("owner", "Owner"), ("editor", "Editor"), ("viewer", "Viewer")]
    ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="viewer")
    is_active = models.BooleanField(default=True)
    # TODO(gatehouse): filled in when a member first signs in through Gatehouse.
    gatehouse_subject = models.CharField(max_length=255, blank=True)
    invite_token = models.CharField(max_length=64, blank=True, db_index=True)
    invited_at = models.DateTimeField(null=True, blank=True)

    objects = TeamMemberManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.name or self.email

    def has_role(self, role):
        return self.ROLE_RANK.get(self.role, 0) >= self.ROLE_RANK[role]

    @property
    def is_owner(self):
        return self.role == "owner"

    @property
    def can_edit(self):
        return self.has_role("editor")

    @property
    def first_name(self):
        return (self.name or self.email).split(" ")[0].split("@")[0]

    @property
    def initial(self):
        return (self.name or self.email or "?")[0].upper()

    # Django admin-ish compatibility (the admin is not installed).
    @property
    def is_staff(self):
        return self.is_owner


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key(kind: str):
    """Return (full_key, prefix, hash). The full key is shown once and never stored."""
    key = f"po_{kind}_{secrets.token_urlsafe(30)}"
    return key, key[:14], hash_key(key)


class Building(TimeStamped, PublicIdMixin):
    """Another Hamlet app connected to Post Office."""

    PUBLIC_PREFIX = "bld"
    KIND_CHOICES = [
        ("town_hall", "Town Hall"),
        ("pavilion", "Pavilion"),
        ("bell_tower", "Bell Tower"),
        ("other", "Other"),
    ]

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="other")
    base_url = models.URLField(blank=True)
    key_hash = models.CharField(max_length=64, blank=True, db_index=True)
    key_prefix = models.CharField(max_length=20, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def issue_key(self):
        key, self.key_prefix, self.key_hash = generate_key("bld")
        self.save(update_fields=["key_prefix", "key_hash", "updated_at"])
        return key


class ApiKey(TimeStamped, PublicIdMixin):
    """A key for something outside Hamlet, limited by scope."""

    PUBLIC_PREFIX = "key"
    SCOPE_CHOICES = [("send", "Send"), ("subscribe", "Subscribe"), ("all", "All")]

    name = models.CharField(max_length=120)
    key_hash = models.CharField(max_length=64, db_index=True)
    key_prefix = models.CharField(max_length=20)
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default="send")
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def allows(self, scope):
        return self.revoked_at is None and self.scope in (scope, "all")

    @classmethod
    def issue(cls, name, scope):
        key, prefix, digest = generate_key("key")
        return cls.objects.create(name=name, scope=scope, key_prefix=prefix, key_hash=digest), key
