from django.db import models

from core.models import PublicIdMixin, TimeStamped


class Campaign(TimeStamped, PublicIdMixin):
    """A message staff write and send to campaign lists. Message type: newsletter."""

    PUBLIC_PREFIX = "cmp"
    STATE_CHOICES = [
        ("draft", "Draft"),
        ("scheduled", "Scheduled"),
        ("sending", "Sending"),
        ("sent", "Sent"),
        ("paused", "Paused"),
        ("cancelled", "Cancelled"),
    ]

    name = models.CharField(max_length=200)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default="draft")
    lists = models.ManyToManyField("people.List", blank=True, related_name="campaigns")
    channels = models.JSONField(default=list, blank=True)  # ["email", "telegram", "push"]
    sender = models.ForeignKey("channels.EmailSender", null=True, blank=True, on_delete=models.SET_NULL)
    bot = models.ForeignKey("channels.TelegramBot", null=True, blank=True, on_delete=models.SET_NULL)
    push_apps = models.ManyToManyField("channels.PushApp", blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    spread_minutes = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey("core.TeamMember", null=True, blank=True, on_delete=models.SET_NULL)
    started_at = models.DateTimeField(null=True, blank=True)  # when fan-out began
    sent_at = models.DateTimeField(null=True, blank=True)
    # Kept after message rows are deleted (180-day retention).
    totals = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def is_editable(self):
        return self.state in ("draft", "scheduled")
