from django.db import models
from django.db.models import Q

from core.models import Language, PublicIdMixin, TimeStamped

FORM_CHOICES = [("email", "Email"), ("notification", "Notification")]
MESSAGE_TYPES = [("newsletter", "Newsletter"), ("service", "Service")]


class Template(TimeStamped, PublicIdMixin):
    """A campaign template (a starting point) or an app message a building triggers."""

    PUBLIC_PREFIX = "tpl"
    KIND_CHOICES = [("campaign", "Campaign template"), ("app", "App message")]

    key = models.CharField(max_length=120, unique=True)  # e.g. pavilion.order_confirmation
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="app")
    building = models.ForeignKey("core.Building", null=True, blank=True, on_delete=models.CASCADE, related_name="templates")
    name = models.CharField(max_length=160)
    fires = models.CharField(max_length=240, blank=True)  # "When an order is placed"
    message_type = models.CharField(max_length=12, choices=MESSAGE_TYPES, default="service")
    # {"order.number": {"label": "order number", "sample": "#0142"},
    #  "order.items": {"label": "order items", "list": {"name": {}, "quantity": {}}}}
    variables = models.JSONField(default=dict, blank=True)
    # The building's own content, kept so "Reset to default" can restore it:
    # {"en": {"email": {"subject": "...", "blocks": []}, "notification": {"text": "..."}}}
    default_content = models.JSONField(default=dict, blank=True)
    customised = models.BooleanField(default=False)

    class Meta:
        ordering = ["building__name", "name"]

    def __str__(self):
        return self.name

    def content_for(self, code, form):
        return self.contents.filter(language=code, form=form).first()

    def has_form(self, form):
        return any(c.is_written for c in self.contents.filter(form=form))


class QuickMessage(TimeStamped, PublicIdMixin):
    """A one-off message staff send from a small dialog to people or lists."""

    PUBLIC_PREFIX = "qm"

    created_by = models.ForeignKey("core.TeamMember", null=True, on_delete=models.SET_NULL)
    lists = models.ManyToManyField("people.List", blank=True, related_name="quick_messages")
    people = models.ManyToManyField("people.Person", blank=True, related_name="quick_messages")
    channels = models.JSONField(default=list)  # ["email", "telegram", "push"]; empty means every channel they use
    message_type = models.CharField(max_length=12, choices=MESSAGE_TYPES, default="service")
    sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        c = self.contents.first()
        return (c.text[:60] if c else "") or f"Quick message {self.pk}"


class Content(TimeStamped):
    """What a template, campaign or quick message says in one language, in one form."""

    MODE_CHOICES = [("visual", "Visual"), ("html", "HTML")]

    # Exactly one owner is set.
    template = models.ForeignKey(Template, null=True, blank=True, on_delete=models.CASCADE, related_name="contents")
    campaign = models.ForeignKey("campaigns.Campaign", null=True, blank=True, on_delete=models.CASCADE, related_name="contents")
    quick_message = models.ForeignKey(QuickMessage, null=True, blank=True, on_delete=models.CASCADE, related_name="contents")

    language = models.CharField(max_length=16)
    form = models.CharField(max_length=14, choices=FORM_CHOICES)
    # Email form
    subject = models.CharField(max_length=300, blank=True)
    preview_text = models.CharField(max_length=300, blank=True)
    mode = models.CharField(max_length=8, choices=MODE_CHOICES, default="visual")
    blocks = models.JSONField(default=list, blank=True)
    html = models.TextField(blank=True)
    # Notification form (Telegram keeps the marks; push strips them)
    text = models.TextField(blank=True)
    button_label = models.CharField(max_length=120, blank=True)
    button_url = models.CharField(max_length=500, blank=True)
    image = models.ImageField(upload_to="n/", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template", "language", "form"], condition=Q(template__isnull=False), name="content_unique_template"),
            models.UniqueConstraint(fields=["campaign", "language", "form"], condition=Q(campaign__isnull=False), name="content_unique_campaign"),
            models.UniqueConstraint(fields=["quick_message", "language", "form"], condition=Q(quick_message__isnull=False), name="content_unique_quick"),
        ]

    @property
    def owner(self):
        return self.template or self.campaign or self.quick_message

    @property
    def is_written(self):
        """Email: a subject and at least one block (or HTML). Notification: non-empty text."""
        if self.form == "email":
            body = bool(self.html.strip()) if self.mode == "html" else bool(self.blocks)
            return bool(self.subject.strip()) and body
        return bool(self.text.strip())


def owner_filter(owner):
    """Keyword for Content queries: {'campaign': owner} and so on."""
    from campaigns.models import Campaign

    if isinstance(owner, Template):
        return {"template": owner}
    if isinstance(owner, Campaign):
        return {"campaign": owner}
    if isinstance(owner, QuickMessage):
        return {"quick_message": owner}
    raise TypeError(f"{owner!r} cannot own content")


def get_content(owner, code, form, create=False):
    kw = owner_filter(owner)
    obj = Content.objects.filter(language=code, form=form, **kw).first()
    if obj is None and create:
        obj = Content.objects.create(language=code, form=form, **kw)
    return obj


def written_languages(owner, form):
    """Codes of the languages in which this owner's form is written."""
    return [c.language for c in Content.objects.filter(form=form, **owner_filter(owner)) if c.is_written]


def pick_content(owner, form, person_code):
    """The content a person gets and whether it is a fallback.

    Returns (content, used_fallback). content is None when neither the person's
    language nor the default language is written. The "hold" rule is enforced
    where campaigns are scheduled, not here.
    """
    contents = {c.language: c for c in Content.objects.filter(form=form, **owner_filter(owner)) if c.is_written}
    if person_code in contents:
        return contents[person_code], False
    default = Language.default_code()
    if default in contents:
        return contents[default], True
    return None, False
