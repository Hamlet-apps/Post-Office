import secrets

from django.db import models

from core.models import PublicIdMixin, TimeStamped
from people.models import CHANNEL_CHOICES


def _empty_list():
    return []


def _token():
    return secrets.token_urlsafe(16)


class SendRequest(TimeStamped, PublicIdMixin):
    """One POST /api/v1/messages call. Its Message rows are the per-channel results."""

    PUBLIC_PREFIX = "msg"

    building = models.ForeignKey("core.Building", null=True, blank=True, on_delete=models.CASCADE)
    api_key = models.ForeignKey("core.ApiKey", null=True, blank=True, on_delete=models.SET_NULL)
    template = models.ForeignKey("content.Template", on_delete=models.CASCADE)
    person = models.ForeignKey("people.Person", on_delete=models.CASCADE)
    idempotency_key = models.CharField(max_length=200)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [("building", "idempotency_key")]


class Message(TimeStamped, PublicIdMixin):
    """The outbox: one row per person per channel."""

    PUBLIC_PREFIX = "m"
    SOURCE_CHOICES = [("campaign", "Campaign"), ("building", "Building"), ("quick", "Quick message"), ("system", "Post Office")]
    STATE_CHOICES = [
        ("queued", "Waiting"),
        ("sending", "Sending"),
        ("sent", "Delivered"),
        ("opened", "Opened"),
        ("clicked", "Clicked"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    ]
    DELIVERED_STATES = ("sent", "opened", "clicked")
    MESSAGE_TYPES = [("newsletter", "Newsletter"), ("service", "Service")]

    person = models.ForeignKey("people.Person", null=True, on_delete=models.CASCADE, related_name="messages")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    message_type = models.CharField(max_length=12, choices=MESSAGE_TYPES, default="service")
    campaign = models.ForeignKey("campaigns.Campaign", null=True, blank=True, on_delete=models.CASCADE, related_name="messages")
    template = models.ForeignKey("content.Template", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    quick_message = models.ForeignKey("content.QuickMessage", null=True, blank=True, on_delete=models.CASCADE, related_name="messages")
    request = models.ForeignKey(SendRequest, null=True, blank=True, on_delete=models.CASCADE, related_name="messages")
    building = models.ForeignKey("core.Building", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    # The list whose subscription let this message through (used by one-click unsubscribe).
    list = models.ForeignKey("people.List", null=True, blank=True, on_delete=models.SET_NULL)
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    language = models.CharField(max_length=16, blank=True)
    used_fallback = models.BooleanField(default=False)
    sender = models.ForeignKey("channels.EmailSender", null=True, blank=True, on_delete=models.SET_NULL)
    bot = models.ForeignKey("channels.TelegramBot", null=True, blank=True, on_delete=models.SET_NULL)
    push_app = models.ForeignKey("channels.PushApp", null=True, blank=True, on_delete=models.SET_NULL)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default="queued", db_index=True)
    error = models.TextField(blank=True)  # the reason, in words staff understand
    warnings = models.JSONField(default=_empty_list, blank=True)  # e.g. unknown variables at render time
    attempts = models.PositiveIntegerField(default=0)
    send_after = models.DateTimeField(null=True, blank=True, db_index=True)
    sending_since = models.DateTimeField(null=True, blank=True)
    requeued = models.BooleanField(default=False)  # a stuck service message is re-queued once
    idempotency_key = models.CharField(max_length=255, unique=True)  # <source id>:<person id>:<channel>
    token = models.CharField(max_length=32, unique=True, default=_token)  # open pixel and unsubscribe links
    subject_snapshot = models.CharField(max_length=300, blank=True)
    data = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["state", "send_after"]),
            models.Index(fields=["campaign", "channel", "state"]),
        ]

    @property
    def is_delivered(self):
        return self.state in self.DELIVERED_STATES

    @property
    def title(self):
        """What the Outbox shows in the "message" column."""
        if self.subject_snapshot:
            return self.subject_snapshot
        if self.campaign_id:
            return self.campaign.name
        if self.template_id:
            return self.template.name
        return "Quick message" if self.quick_message_id else "Message"


class TrackedLink(TimeStamped):
    campaign = models.ForeignKey("campaigns.Campaign", on_delete=models.CASCADE, related_name="links")
    url = models.TextField()
    token = models.CharField(max_length=32, unique=True, default=_token)

    class Meta:
        unique_together = [("campaign", "url")]


class LinkClick(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="clicks")
    link = models.ForeignKey(TrackedLink, on_delete=models.CASCADE, related_name="clicks")
    at = models.DateTimeField(auto_now_add=True)
