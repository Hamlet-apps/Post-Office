"""Shared test data: the fictional Maple Grove Community Centre. Nothing here touches the network."""
from unittest import mock

from campaigns.models import Campaign
from channels.models import EmailSender, PushApp, PushMember, TelegramBot, TelegramLink
from content.models import Content, Template
from core.models import Building, Language, Organization
from people.models import List, Person
from people.services import record_consent, set_subscription


def make_org():
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.reply_to = "hello@maplegrove.example"
    org.mailing_address = "12 Alder Street, Maple Grove"
    org.timezone = "America/Vancouver"
    org.setup_complete = True
    org.save()
    for position, (code, name, default) in enumerate([("en", "English", True), ("uk", "Українська", False), ("fr", "Français", False)]):
        Language.objects.update_or_create(code=code, defaults={"name": name, "is_default": default, "position": position})
    return org


def make_sender(purpose="newsletter", **extra):
    values = dict(purpose=purpose, from_name="Maple Grove News", from_email=f"{purpose}@maplegrove.example", kind="smtp",
                  host="smtp.maplegrove.example", port=587, username="news", password="secret", state="ok",
                  is_default=purpose == "newsletter", per_second=50)
    values.update(extra)
    return EmailSender.objects.create(**values)


def make_bot(**extra):
    values = dict(name="Maple Grove", username="MapleGroveBot", token="123:abc", is_default=True)
    values.update(extra)
    return TelegramBot.objects.create(**values)


def make_list(slug="newsletter", kind="campaign", building=None, name="Newsletter"):
    return List.objects.create(slug=slug, kind=kind, building=building, name={"en": name})


def make_person(email="olena.k@example.com", first_name="Olena", language="en", **extra):
    return Person.objects.create(email=email, first_name=first_name, language=Language.objects.get(code=language), **extra)


def subscribe(person, lst, channel="email", unconfirmed=False):
    consent = None
    if lst.kind == "campaign":
        consent = record_consent(person, [lst], "signup_form", "Yes, send me the newsletter.", confirmed=not unconfirmed)
    return set_subscription(person, lst, channel, True, consent=consent, unconfirmed=unconfirmed)


def connect_telegram(person, bot, chat_id=None):
    return TelegramLink.objects.create(person=person, bot=bot, chat_id=chat_id or 1000 + person.pk)


def make_campaign(lists, channels=("email",), sender=None, bot=None, languages=("en",), name="Winter coat drive", **extra):
    campaign = Campaign.objects.create(name=name, channels=list(channels), sender=sender, bot=bot, state="scheduled", **extra)
    campaign.lists.set(lists)
    for code in languages:
        Content.objects.create(campaign=campaign, language=code, form="email", subject=f"Coat drive ({code})", blocks=[
            {"id": "b1", "type": "text", "text": "Hi {first_name}, see [the calendar](https://maplegrove.example/calendar)."}])
        Content.objects.create(campaign=campaign, language=code, form="notification",
                               text=f"*Coat drive* this Saturday ({code})", button_label="Details",
                               button_url="https://maplegrove.example/coats")
    return campaign


def make_building(name="Pavilion", kind="pavilion"):
    building = Building.objects.create(name=name, kind=kind, base_url=f"https://{kind}.maplegrove.example")
    key = building.issue_key()
    return building, key


def make_template(building, key="pavilion.order_confirmation", languages=("en",), notification=True):
    template = Template.objects.create(key=key, kind="app", building=building, name="Order confirmation",
                                       message_type="service")
    for code in languages:
        Content.objects.create(template=template, language=code, form="email", subject="Order {order.number} ({first_name})",
                               blocks=[{"id": "b1", "type": "text", "text": "Ready at {order.ready_time}."}])
        if notification:
            Content.objects.create(template=template, language=code, form="notification", text="Order {order.number} is in.")
    return template


def make_push_app(building, lst, audience="offers-and-news"):
    return PushApp.objects.create(building=building, audience=audience, label="Offers and news",
                                  endpoint="https://pavilion.maplegrove.example/api/notifications", secret="shared-secret", list=lst)


def allow_push(person, app, external_id=None):
    return PushMember.objects.create(person=person, push_app=app, external_id=external_id or f"cust_{person.pk}")


class FakeSMTP:
    """Stands in for smtplib.SMTP: records what was sent, can be told to fail."""

    def __init__(self):
        self.sent = []  # (to_address, EmailMessage)
        self.fail_with = None  # an exception to raise on send
        self.connections = 0
        self.patch = mock.patch("smtplib.SMTP", side_effect=self._connect)

    def _connect(self, *args, **kwargs):
        self.connections += 1
        smtp = mock.MagicMock()
        smtp.send_message.side_effect = self._send
        return smtp

    def _send(self, message, from_addr=None, to_addrs=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((to_addrs[0], message))
        return {}

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()

    def html(self, index=-1):
        return self.sent[index][1].get_body(preferencelist=("html",)).get_content()

    def text(self, index=-1):
        return self.sent[index][1].get_body(preferencelist=("plain",)).get_content()
