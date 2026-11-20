"""Sending limits (spec section 7), kept in the Django cache so they work with and without Redis.

- Email: `per_second` tokens per sender, refilled every second, and an optional
  daily counter in the organization's time zone.
- Telegram: 25 messages per second per bot.
- Push: one request at a time per app, up to 500 messages per request.
"""
import datetime
import time

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from core.models import Organization

TELEGRAM_PER_SECOND = 25
PUSH_BATCH = 500
_DAY = 60 * 60 * 48


# --------------------------------------------------------------------------- per-second buckets

def _take(key, capacity):
    """Take one token from this second's bucket. Returns True when there was one."""
    bucket = f"po:rate:{key}:{int(time.time())}"
    cache.add(bucket, 0, 5)
    try:
        used = cache.incr(bucket)
    except ValueError:  # the key expired between add and incr
        cache.set(bucket, 1, 5)
        used = 1
    return used <= max(1, int(capacity or 1))


def _wait_for(key, capacity, max_wait):
    """Take a token, waiting for the next second if this one is used up."""
    deadline = time.monotonic() + max_wait
    while True:
        if _take(key, capacity):
            return True
        pause = 1.0 - (time.time() % 1.0) + 0.01
        if time.monotonic() + pause > deadline:
            return False
        time.sleep(pause)


def take_email_token(sender, max_wait=3.0):
    return _wait_for(f"snd{sender.pk}", sender.per_second or 5, max_wait)


def take_telegram_token(bot, max_wait=3.0):
    return _wait_for(f"bot{bot.pk}", TELEGRAM_PER_SECOND, max_wait)


# --------------------------------------------------------------------------- daily limit

def local_day_start(org=None, now=None):
    org = org or Organization.get()
    local = (now or timezone.now()).astimezone(org.tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def next_day_start(org=None, now=None):
    """00:05 tomorrow in the organization's time zone: when held messages go out."""
    org = org or Organization.get()
    tomorrow = (local_day_start(org, now) + datetime.timedelta(days=1)).date()
    return datetime.datetime.combine(tomorrow, datetime.time(0, 5), tzinfo=org.tz)


def _daily_key(sender, org=None, now=None):
    return f"po:daily:snd{sender.pk}:{local_day_start(org, now):%Y%m%d}"


def _sent_today_from_rows(sender, org=None):
    from .models import Message

    return Message.objects.filter(sender=sender, state__in=Message.DELIVERED_STATES,
                                  sent_at__gte=local_day_start(org)).count()


def reserve_daily(sender, org=None):
    """Count one email against today's limit. False means the limit is reached."""
    if not sender.daily_limit:
        return True
    key = _daily_key(sender, org)
    cache.add(key, _sent_today_from_rows(sender, org), _DAY)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, _DAY)
        used = 1
    if used > sender.daily_limit:
        release_daily(sender, org)
        return False
    return True


def release_daily(sender, org=None):
    """Give back a reservation for an email that did not go out."""
    if not sender.daily_limit:
        return
    try:
        cache.decr(_daily_key(sender, org))
    except ValueError:
        pass


def sender_status(sender):
    """For the Channels card: "1,500 sent today, 918 waiting until tomorrow"."""
    from .models import Message

    org = Organization.get()
    waiting = Message.objects.filter(sender=sender, state="queued",
                                     send_after__gte=local_day_start(org) + datetime.timedelta(days=1)).count()
    return {"sent_today": _sent_today_from_rows(sender, org), "waiting": waiting}


# --------------------------------------------------------------------------- push: one request at a time

def push_lock(push_app, seconds=120):
    return cache.add(f"po:pushlock:{push_app.pk}", 1, seconds)


def push_unlock(push_app):
    cache.delete(f"po:pushlock:{push_app.pk}")


def due_filter(now=None):
    """Q for messages whose send_after has passed (or was never set)."""
    now = now or timezone.now()
    return Q(send_after__isnull=True) | Q(send_after__lte=now)
