import secrets

from django.db import models

from core.models import PublicIdMixin, TimeStamped

BUILT_IN_COMMANDS = ["start", "stop", "language", "help"]


def _token():
    return secrets.token_urlsafe(24)


class Command(TimeStamped, PublicIdMixin):
    """A Telegram slash command that Post Office passes to a web address.

    TODO(switchboard): the spec has a second target, Switchboard (pick a workflow,
    post the standard payload to its trigger address). Switchboard is not built
    yet, so that target is left out; "http" is the only one for now.
    """

    PUBLIC_PREFIX = "cmd"
    TARGET_CHOICES = [("http", "Web address")]
    METHOD_CHOICES = [(m, m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")]
    REPLY_CHOICES = [("answer", "Send back the answer"), ("none", "No reply")]

    bot = models.ForeignKey("channels.TelegramBot", on_delete=models.CASCADE, related_name="commands")
    name = models.CharField(max_length=32)  # lowercase letters, digits, underscores
    descriptions = models.JSONField(default=dict, blank=True)  # per language
    target = models.CharField(max_length=12, choices=TARGET_CHOICES, default="http")
    method = models.CharField(max_length=6, choices=METHOD_CHOICES, default="POST")
    url = models.TextField(blank=True)
    # [{"name": "Authorization", "value": "<encrypted with core.fields.encrypt>"}]
    headers = models.JSONField(default=list, blank=True)
    body = models.TextField(blank=True)
    reply_path = models.CharField(max_length=200, blank=True)  # dot path into the JSON answer
    reply = models.CharField(max_length=8, choices=REPLY_CHOICES, default="answer")
    connected_only = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = [("bot", "name")]
        ordering = ["name"]

    def __str__(self):
        return f"/{self.name}"


class BotText(TimeStamped):
    """A bot's own replies, per language."""

    bot = models.ForeignKey("channels.TelegramBot", on_delete=models.CASCADE, related_name="texts")
    language = models.CharField(max_length=16)
    unknown_command = models.TextField(blank=True)
    no_answer = models.TextField(blank=True)

    class Meta:
        unique_together = [("bot", "language")]


class CommandLog(TimeStamped, PublicIdMixin):
    PUBLIC_PREFIX = "log"
    RESULT_CHOICES = [("answered", "Answered"), ("no_answer", "No answer"), ("unknown", "Unknown command"), ("error", "Error")]

    bot = models.ForeignKey("channels.TelegramBot", on_delete=models.CASCADE, related_name="command_logs")
    person = models.ForeignKey("people.Person", null=True, blank=True, on_delete=models.SET_NULL)
    chat_id = models.BigIntegerField()
    text = models.TextField(blank=True)
    command = models.CharField(max_length=64, blank=True)
    target = models.CharField(max_length=20, blank=True)  # built_in or http
    result = models.CharField(max_length=12, choices=RESULT_CHOICES, default="answered")
    duration_ms = models.PositiveIntegerField(default=0)
    # A target may answer later, once, through reply_url within 24 hours.
    reply_token = models.CharField(max_length=48, default=_token)
    late_replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
