from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Subscription

UNCONFIRMED_DAYS = 14


@shared_task
def delete_unconfirmed_subscriptions():
    """Subscriptions nobody confirmed within 14 days are deleted (spec section 8)."""
    cutoff = timezone.now() - timedelta(days=UNCONFIRMED_DAYS)
    deleted, _ = Subscription.objects.filter(state="unconfirmed", updated_at__lt=cutoff).delete()
    return deleted
