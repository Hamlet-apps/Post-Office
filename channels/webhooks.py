"""Webhooks: a signed JSON copy of every campaign at send time (spec section 6).

For lobby displays and chat rooms, not for per-person delivery.
"""
import logging

import httpx
from django.utils import timezone

from .models import Webhook
from .push import signed_post

log = logging.getLogger(__name__)


def campaign_payload(campaign):
    """Name, per-language subject and notification text, and the lists."""
    content = {}
    for c in campaign.contents.all():
        if not c.is_written:
            continue
        entry = content.setdefault(c.language, {})
        if c.form == "email":
            entry["subject"] = c.subject
            if c.preview_text:
                entry["preview_text"] = c.preview_text
        else:
            entry["notification"] = c.text
            if c.button_url:
                entry["button"] = {"label": c.button_label, "url": c.button_url}
    return {
        "event": "campaign.sent",
        "id": campaign.public_id,
        "name": campaign.name,
        "sent_at": (campaign.started_at or timezone.now()).isoformat(),
        "channels": list(campaign.channels or []),
        "lists": [{"slug": lst.slug, "name": lst.name} for lst in campaign.lists.all()],
        "content": content,
    }


def post_to(webhook, payload):
    """Post one payload to one webhook. Returns (ok, status_text) and remembers the status."""
    try:
        response = signed_post(webhook.url, payload, webhook.secret, timeout=10)
        ok = response.status_code < 400
        status = f"{response.status_code}"
    except httpx.HTTPError as exc:
        ok, status = False, f"could not reach it ({exc.__class__.__name__})"
    except Exception as exc:  # noqa: BLE001 - a webhook must never stop a campaign
        log.exception("webhook %s failed", webhook.pk)
        ok, status = False, exc.__class__.__name__
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    Webhook.objects.filter(pk=webhook.pk).update(
        last_status=(f"ok {status} at {stamp}" if ok else f"failed: {status} at {stamp}")[:200])
    return ok, status


def post_campaign(campaign):
    """Post the campaign to every enabled webhook. Returns how many accepted it."""
    hooks = list(Webhook.objects.filter(enabled=True))
    if not hooks:
        return 0
    payload = campaign_payload(campaign)
    return sum(1 for hook in hooks if post_to(hook, payload)[0])
