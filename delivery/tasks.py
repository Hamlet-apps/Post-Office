"""Celery tasks for the sending pipeline.

With no Redis (development and tests) Celery runs eagerly, so every task here
is an ordinary function call and none of them calls itself: a message that is
not due yet simply stays queued until send_due_messages finds it.
"""
from celery import shared_task


@shared_task(name="delivery.tasks.send_batch")
def send_batch(message_ids):
    """Send one chunk of messages (up to 100 per channel, 500 for a push app)."""
    from .pipeline import deliver_batch

    return deliver_batch(message_ids)


@shared_task(name="delivery.tasks.send_due_messages")
def send_due_messages():
    """Beat, every 15 seconds: queue messages whose send_after has passed."""
    from .pipeline import send_due

    return send_due()


@shared_task(name="delivery.tasks.recover_stuck_messages")
def recover_stuck_messages():
    """Beat, every 5 minutes: deal with messages a dead worker left in "sending"."""
    from .pipeline import recover_stuck

    return list(recover_stuck())


@shared_task(name="delivery.tasks.apply_retention")
def apply_retention():
    """Beat, daily: delete message rows older than 180 days, keeping campaign totals."""
    from .pipeline import apply_retention as run

    return run()
