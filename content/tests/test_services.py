import pytest

from campaigns.models import Campaign
from content.models import Content, Template, get_content
from content.services import (
    clean_blocks,
    copy_content,
    copy_language,
    language_states,
    register_templates,
    reset_to_default,
    save_as_campaign_template,
)
from people.models import Person

pytestmark = pytest.mark.django_db


def test_register_templates_creates_them_with_their_content(pavilion, order_definition):
    assert register_templates(pavilion, [order_definition]) == {"created": 1, "updated": 0, "kept": 0}
    template = Template.objects.get(key="pavilion.order_confirmation")
    assert template.kind == "app" and template.building == pavilion and template.message_type == "service"
    assert template.fires == "When an order is placed" and "order.items" in template.variables
    email = get_content(template, "en", "email")
    assert email.subject == "Your order {order.number} is in"
    assert [b["type"] for b in email.blocks] == ["heading", "repeat"]
    assert get_content(template, "en", "notification").text == "Order {order.number} is in."
    assert not template.customised


def test_register_templates_updates_the_defaults_of_an_untouched_template(pavilion, order_definition):
    register_templates(pavilion, [order_definition])
    order_definition["name"] = "Order received"
    order_definition["content"]["en"]["email"]["subject"] = "We have your order {order.number}"
    assert register_templates(pavilion, [order_definition]) == {"created": 0, "updated": 1, "kept": 0}
    template = Template.objects.get(key="pavilion.order_confirmation")
    assert template.name == "Order received"
    assert get_content(template, "en", "email").subject == "We have your order {order.number}"


def test_register_templates_never_overwrites_a_customised_template_and_reset_restores_it(pavilion, order_definition):
    register_templates(pavilion, [order_definition])
    template = Template.objects.get(key="pavilion.order_confirmation")
    email = get_content(template, "en", "email")
    email.subject = "Maple Grove has your order"
    email.save()
    Content.objects.create(template=template, language="uk", form="email", subject="Ваше замовлення",
                           blocks=[{"id": "x", "type": "text", "text": "Дякуємо"}])
    template.customised = True
    template.save()

    order_definition["content"]["en"]["email"]["subject"] = "New default {order.number}"
    assert register_templates(pavilion, [order_definition]) == {"created": 0, "updated": 0, "kept": 1}
    template.refresh_from_db()
    assert get_content(template, "en", "email").subject == "Maple Grove has your order"  # staff wording kept
    assert template.default_content["en"]["email"]["subject"] == "New default {order.number}"  # default still updated
    assert template.customised

    reset_to_default(template)
    template.refresh_from_db()
    assert not template.customised
    assert get_content(template, "en", "email").subject == "New default {order.number}"
    assert get_content(template, "uk", "email") is None  # what staff added is gone with the reset


def test_a_language_with_no_content_is_reported_as_not_written():
    campaign = Campaign.objects.create(name="December newsletter")
    Content.objects.create(campaign=campaign, language="en", form="email", subject="December at Maple Grove",
                           blocks=[{"id": "b1", "type": "heading", "text": "December"}])
    Content.objects.create(campaign=campaign, language="uk", form="email", subject="", blocks=[])  # started, not written
    Person.objects.create(email="olena@example.com", first_name="Olena")
    states = {s["code"]: s for s in language_states(campaign, "email")}
    assert states["en"]["written"] and states["en"]["is_default"] and states["en"]["readers"] == 1
    assert not states["uk"]["written"]
    assert not states["fr"]["written"] and states["fr"]["readers"] == 0
    assert not any(s["written"] for s in language_states(campaign, "notification"))


def test_copy_from_default_copies_the_blocks():
    campaign = Campaign.objects.create(name="December newsletter")
    blocks = [{"id": "b1", "type": "heading", "text": "December"}, {"id": "b2", "type": "text", "text": "Hi {first_name}"}]
    Content.objects.create(campaign=campaign, language="en", form="email", subject="December", blocks=blocks)
    Content.objects.create(campaign=campaign, language="en", form="notification", text="*December* is busy")
    assert copy_language(campaign, "fr") == ["email", "notification"]
    french = get_content(campaign, "fr", "email")
    assert french.blocks == blocks and french.subject == "December" and french.is_written
    assert get_content(campaign, "fr", "notification").text == "*December* is busy"
    # A language that is already written is left alone.
    french.subject = "Décembre"
    french.save()
    assert copy_language(campaign, "fr") == []
    assert get_content(campaign, "fr", "email").subject == "Décembre"


def test_copy_content_and_save_as_campaign_template():
    campaign = Campaign.objects.create(name="December newsletter")
    Content.objects.create(campaign=campaign, language="en", form="email", subject="December",
                           blocks=[{"id": "b1", "type": "heading", "text": "December"}])
    Content.objects.create(campaign=campaign, language="uk", form="notification", text="Грудень")

    template = save_as_campaign_template(campaign, "Monthly newsletter")
    assert template.kind == "campaign" and template.message_type == "newsletter"
    assert template.key == "campaign.monthly-newsletter"
    assert get_content(template, "en", "email").blocks[0]["text"] == "December"
    assert get_content(template, "uk", "notification").text == "Грудень"
    assert save_as_campaign_template(campaign, "Monthly newsletter").key == "campaign.monthly-newsletter-2"

    fresh = Campaign.objects.create(name="January newsletter")
    copy_content(template, fresh)
    assert get_content(fresh, "en", "email").subject == "December"
    assert Content.objects.filter(campaign=fresh).count() == 2
    assert Content.objects.filter(campaign=campaign).count() == 2  # the source keeps its own rows


def test_clean_blocks_keeps_known_fields_and_gives_every_block_an_id():
    blocks = clean_blocks([
        {"id": "b1", "type": "heading", "text": "Hello", "align": "sideways", "onclick": "x"},
        {"id": "b1", "type": "text", "text": "Same id twice"},
        {"type": "script", "text": "nope"},
        {"type": "repeat", "source": "order.items", "show": ["name"]},
        "not a block",
    ])
    assert [b["type"] for b in blocks] == ["heading", "text"]  # no repeat blocks outside app templates
    assert blocks[0] == {"id": "b1", "type": "heading", "text": "Hello", "align": "left"}
    assert blocks[1]["id"] != "b1"
    assert clean_blocks([{"type": "repeat", "source": "order.items", "show": ["name"]}], allow_repeat=True)[0]["show"] == ["name"]
