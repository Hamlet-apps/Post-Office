"""Telegram long polling (spec section 6): a beat task every 2 seconds."""
import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

from . import telegram
from .models import TelegramBot

log = logging.getLogger(__name__)


def handle_update(bot, update):
    """Hand one Telegram update to the commands area. Never raises."""
    try:
        from commands.handler import process_update
    except ImportError:
        log.warning("commands.handler is not available; Telegram update ignored")
        return False
    try:
        process_update(bot, update)
        return True
    except Exception:  # noqa: BLE001 - one bad update must not block the ones after it
        log.exception("Telegram update %s for bot %s failed", update.get("update_id"), bot.pk)
        return False


def poll_bot(bot):
    """Fetch and handle this bot's new updates. Returns how many there were."""
    lock = f"po:tgpoll:{bot.pk}"
    if not cache.add(lock, 1, 30):
        return 0  # the previous poll of this bot is still running
    try:
        try:
            updates = telegram.TelegramClient(bot).get_updates(offset=(bot.last_update_id + 1) if bot.last_update_id else None)
        except telegram.TelegramError as error:
            log.warning("polling bot %s failed: %s", bot.pk, error)
            return 0
        for update in updates:
            handle_update(bot, update)
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id > bot.last_update_id:
                bot.last_update_id = update_id
                TelegramBot.objects.filter(pk=bot.pk).update(last_update_id=update_id)
        return len(updates)
    finally:
        cache.delete(lock)


@shared_task(name="channels.tasks.poll_telegram")
def poll_telegram():
    if settings.TELEGRAM_MODE != "polling":
        return 0
    return sum(poll_bot(bot) for bot in TelegramBot.objects.all())
