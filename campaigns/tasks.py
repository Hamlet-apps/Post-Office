from celery import shared_task


@shared_task(name="campaigns.tasks.start_scheduled_campaigns")
def start_scheduled_campaigns():
    """Beat task, every 30 seconds: start the campaigns whose scheduled time has come."""
    from .services import start_due

    return start_due()
