"""Moving a campaign through its states (spec section 7, "Campaign state").

The sending itself belongs to delivery.pipeline. These helpers call it and make
sure the campaign's state follows, whatever the pipeline did with it:

    draft -> scheduled -> sending -> sent
                          sending <-> paused
    scheduled -> draft (cancel before it starts), sending/paused -> cancelled
"""
import logging

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from content.models import Content

from .models import Campaign

log = logging.getLogger(__name__)
START_LOCK_SECONDS = 600


def _pipeline():
    from delivery import pipeline

    return pipeline


def schedule(campaign, at, spread_minutes=0):
    """Queue the campaign for a moment in the future (or now, when `at` has passed)."""
    campaign.state = "scheduled"
    campaign.scheduled_at = at
    campaign.spread_minutes = spread_minutes
    campaign.save(update_fields=["state", "scheduled_at", "spread_minutes", "updated_at"])
    return campaign


def start(campaign):
    """Start the fan-out of a scheduled campaign. Returns False when another worker already has it.

    A short cache lock stops two beat runs from starting the same campaign;
    the pipeline's idempotency keys make even a repeat harmless.
    """
    lock = f"campaign-start:{campaign.pk}"
    if not cache.add(lock, 1, START_LOCK_SECONDS):
        return False
    try:
        _pipeline().send_campaign(campaign)
    except Exception:
        cache.delete(lock)
        raise
    campaign.refresh_from_db()
    if campaign.state == "scheduled":  # the pipeline left the state to us
        campaign.state = "sending"
    if campaign.started_at is None:
        campaign.started_at = timezone.now()
    campaign.save(update_fields=["state", "started_at", "updated_at"])
    cache.delete(lock)
    return True


def send_now(campaign, spread_minutes=0):
    """Schedule for this moment and start straight away. If the start fails, the beat task retries."""
    schedule(campaign, timezone.now(), spread_minutes)
    return start(campaign)


def start_due(now=None):
    """Start every scheduled campaign whose time has come. Returns how many were started."""
    now = now or timezone.now()
    started = 0
    for campaign in Campaign.objects.filter(state="scheduled", scheduled_at__isnull=False, scheduled_at__lte=now):
        try:
            if start(campaign):
                started += 1
        except Exception:  # one broken campaign must not hold up the others
            log.exception("Could not start campaign %s", campaign.public_id)
    return started


def _settle(campaign, expected_from, to):
    campaign.refresh_from_db()
    if campaign.state in expected_from:
        campaign.state = to
        campaign.save(update_fields=["state", "updated_at"])
    return campaign


def pause(campaign):
    _pipeline().pause_campaign(campaign)
    return _settle(campaign, ("sending",), "paused")


def resume(campaign):
    _pipeline().resume_campaign(campaign)
    return _settle(campaign, ("paused",), "sending")


def cancel(campaign):
    """Before the send starts: back to a draft. After: stop what has not gone out yet."""
    if campaign.state == "scheduled":
        campaign.state = "draft"
        campaign.scheduled_at = None
        campaign.save(update_fields=["state", "scheduled_at", "updated_at"])
        return campaign
    _pipeline().cancel_campaign(campaign)
    return _settle(campaign, ("sending", "paused"), "cancelled")


@transaction.atomic
def duplicate(campaign, user=None, name=None):
    """A new draft with the same content, lists and channels."""
    copy = Campaign.objects.create(
        name=name or campaign.name, state="draft", channels=list(campaign.channels or []),
        sender=campaign.sender, bot=campaign.bot, created_by=user,
    )
    copy.lists.set(campaign.lists.all())
    copy.push_apps.set(campaign.push_apps.all())
    copy_content(campaign, copy)
    return copy


def copy_content(source, target):
    """Copy every Content row. Uses content.services.copy_content when it is there."""
    try:
        from content.services import copy_content as shared

        return shared(source, target)
    except ImportError:
        pass
    from content.models import owner_filter

    for content in Content.objects.filter(**owner_filter(source)):
        content.pk = None
        content.template = content.campaign = content.quick_message = None
        for key, value in owner_filter(target).items():
            setattr(content, key, value)
        content.save()
