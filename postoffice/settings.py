"""Post Office settings. Everything deploy-specific comes from the environment."""
import base64
import hashlib
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return str(env(name, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DEBUG", False)
SECRET_KEY = env("SECRET_KEY") or "dev-only-secret-key-change-me"
ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "*").split(",") if h.strip()]

# Used in tracking, unsubscribe and signup links.
PUBLIC_URL = (env("PUBLIC_URL") or "http://localhost:8000").rstrip("/")
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL] if PUBLIC_URL.startswith("http") else []

# Fernet key for secrets at rest. If ENCRYPTION_KEY is not a valid Fernet key,
# one is derived from it (or from SECRET_KEY in development).
_raw_key = env("ENCRYPTION_KEY") or SECRET_KEY
try:
    if len(base64.urlsafe_b64decode(_raw_key.encode())) != 32:
        raise ValueError
    ENCRYPTION_KEY = _raw_key
except Exception:
    ENCRYPTION_KEY = base64.urlsafe_b64encode(hashlib.sha256(_raw_key.encode()).digest()).decode()

TELEGRAM_MODE = env("TELEGRAM_MODE", "polling")  # polling or webhook
ALLOW_PRIVATE_TARGETS = env_bool("ALLOW_PRIVATE_TARGETS", False)

# TODO(gatehouse): Gatehouse (OpenID Connect) sign-in is not implemented yet.
# Gatehouse itself does not exist yet, so Post Office only offers email and
# password sign-in. These variables are read so deployments can set them ahead
# of time, but nothing uses them. See README.md, "Gatehouse sign-in (to do)".
GATEHOUSE_ISSUER = env("GATEHOUSE_ISSUER", "")
GATEHOUSE_CLIENT_ID = env("GATEHOUSE_CLIENT_ID", "")
GATEHOUSE_CLIENT_SECRET = env("GATEHOUSE_CLIENT_SECRET", "")

# TODO(switchboard): Switchboard is not built yet, so everything that talks to
# it is left out: Telegram commands can only be passed to a web address. When
# Switchboard exists, add it as a second command target (spec section 12).

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "core",
    "people",
    "channels",
    "content",
    "campaigns",
    "delivery",
    "commands",
    "public",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # No LocaleMiddleware: the staff screens are English until their Ukrainian and French
    # catalogues are complete. Reader-facing pages, emails and bot replies choose the
    # reader's language themselves (translation.override).
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.FirstRunMiddleware",
]

ROOT_URLCONF = "postoffice.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "postoffice.wsgi.application"

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'db.sqlite3').as_posix()}",
        conn_max_age=60,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "core.TeamMember"
LOGIN_URL = "/sign-in/"
LOGIN_REDIRECT_URL = "/"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
]

# --- Redis, cache and Celery -------------------------------------------------
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
    CELERY_BROKER_URL = REDIS_URL
    CELERY_TASK_ALWAYS_EAGER = False
else:
    # No Redis (local development and tests): run tasks inline, keep counters in memory.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    CELERY_BROKER_URL = "memory://"
    CELERY_TASK_ALWAYS_EAGER = True
    CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_IGNORE_RESULT = True
CELERY_TIMEZONE = "UTC"
CELERY_BEAT_SCHEDULE = {
    "send-due-messages": {"task": "delivery.tasks.send_due_messages", "schedule": 15.0},
    "start-scheduled-campaigns": {"task": "campaigns.tasks.start_scheduled_campaigns", "schedule": 30.0},
    "recover-stuck-messages": {"task": "delivery.tasks.recover_stuck_messages", "schedule": 300.0},
    "retention": {"task": "delivery.tasks.apply_retention", "schedule": 24 * 3600.0},
    "delete-unconfirmed": {"task": "people.tasks.delete_unconfirmed_subscriptions", "schedule": 24 * 3600.0},
}
if TELEGRAM_MODE == "polling":
    CELERY_BEAT_SCHEDULE["telegram-poll"] = {"task": "channels.tasks.poll_telegram", "schedule": 2.0}

# --- Internationalisation ----------------------------------------------------
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("uk", "Українська"), ("fr", "Français")]
# Languages that ship translated built-in wording (footer, public pages, bot replies).
SHIPPED_TRANSLATIONS = ["en", "uk", "fr"]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = "UTC"

# --- Static and media --------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True  # serve static files straight from the apps when collectstatic has not run
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("MEDIA_ROOT") or BASE_DIR / "media")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
if env("AWS_STORAGE_BUCKET_NAME"):
    # Optional S3-compatible storage for uploaded images.
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["api.auth.BearerKeyAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "api.errors.exception_handler",
    "UNAUTHENTICATED_USER": None,
}

SESSION_COOKIE_SAMESITE = "Lax"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
