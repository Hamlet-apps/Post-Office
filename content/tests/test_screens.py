from unittest import mock

import pytest
from django.urls import reverse

from content.models import Content, QuickMessage, Template
from content.services import register_templates
from core.models import Language
from people.models import List, Person, Subscription

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- Templates screen

def test_templates_screen_lists_campaign_templates_and_app_messages(viewer_client, editor_client, pavilion, order_definition):
    from delivery.models import Message

    register_templates(pavilion, [order_definition])
    order = Template.objects.get(key="pavilion.order_confirmation")
    Content.objects.create(template=order, language="uk", form="email", subject="Замовлення",
                           blocks=[{"id": "a", "type": "text", "text": "Дякуємо"}])
    Content.objects.create(template=order, language="uk", form="notification", text="Замовлення прийнято")
    person = Person.objects.create(email="olena@example.com")
    for n in range(3):
        Message.objects.create(person=person, source="building", template=order, channel="email", idempotency_key=f"k{n}")
    monthly = Template.objects.create(key="campaign.monthly-newsletter", kind="campaign", name="Monthly newsletter",
                                      message_type="newsletter")
    Content.objects.create(template=monthly, language="en", form="email", subject="This month",
                           blocks=[{"id": "a", "type": "heading", "text": "This month"}])

    page = editor_client.get(reverse("content:templates"))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Monthly newsletter" in html and "Start from blank" in html
    assert 'action="/campaigns/new/"' in html and 'name="template" value="campaign.monthly-newsletter"' in html
    assert "Pavilion" in html and "Order confirmation" in html and "When an order is placed" in html
    assert reverse("content:template", args=["pavilion.order_confirmation"]) in html
    assert "Français is not written" in html or "French is not written" in html  # the dashed tag
    assert ">Default<" in html and "Reset to default" not in html

    order = page.context["groups"][0]["templates"][0]
    assert order.sent_30 == 3 and order.has_email and order.has_telegram
    assert {t["code"]: t["written"] for t in order.language_tags} == {"en": True, "uk": True, "fr": False}

    Template.objects.filter(pk=order.pk).update(customised=True)
    html = editor_client.get(reverse("content:templates")).content.decode()
    assert "Customised" in html and "Reset to default" in html

    # A viewer sees the screen but none of the buttons that change things.
    html = viewer_client.get(reverse("content:templates")).content.decode()
    assert "Order confirmation" in html and "Reset to default" not in html and "Start from blank" not in html
    assert viewer_client.post(reverse("content:template_new")).status_code == 403


def test_a_new_campaign_template_opens_in_the_editor(editor_client):
    response = editor_client.post(reverse("content:template_new"))
    template = Template.objects.get(kind="campaign")
    assert response.status_code == 302 and response["Location"] == reverse("content:template", args=[template.key])
    page = editor_client.get(response["Location"])
    assert page.status_code == 200 and "As a push notification" not in page.content.decode()  # email form first
    note = editor_client.get(response["Location"] + "?form=notification").content.decode()
    assert "As a push notification" in note and "On Telegram" in note
    assert editor_client.post(reverse("content:template_delete", args=[template.key])).status_code == 302
    assert not Template.objects.exists()


# --------------------------------------------------------------------------- Quick message

@pytest.fixture
def volunteers():
    """An app list with two English readers and one French reader, plus someone outside it."""
    lst = List.objects.create(name={"en": "Volunteers"}, slug="volunteers", kind="app")
    french = Language.objects.get(code="fr")
    people = [
        Person.objects.create(email="olena@example.com", first_name="Olena", last_name="Kovalenko"),
        Person.objects.create(email="marc@example.com", first_name="Marc"),
        Person.objects.create(email="chloe@example.com", first_name="Chloé", language=french),
    ]
    for person in people:
        Subscription.objects.create(person=person, list=lst, channel="email", state="on")
    outsider = Person.objects.create(email="taras@example.com", first_name="Taras", last_name="Shevchuk")
    return lst, people, outsider


def test_quick_message_dialog_and_search(editor_client, viewer_client, volunteers):
    lst, people, outsider = volunteers
    url = reverse("content:quick_message")
    assert viewer_client.get(url).status_code == 403

    html = editor_client.get(url).content.decode()
    assert html.lstrip().startswith("<dialog") and 'class="po-dialog"' in html and "data-auto-open" in html
    assert "<html" not in html  # only the fragment
    assert "Every channel they use" in html and 'name="text_fr"' in html and "Send now" in html

    found = editor_client.get(reverse("content:quick_message_search"), {"q": "vol"}).content.decode()
    assert "Volunteers · 3" in found and lst.public_id in found
    found = editor_client.get(reverse("content:quick_message_search"), {"q": "taras"}).content.decode()
    assert "Taras Shevchuk" in found and outsider.public_id in found

    summary = editor_client.post(reverse("content:quick_message_summary"), {
        "lists": [lst.public_id], "people": [outsider.public_id, people[0].public_id],
        "text_en": "The parking lot is closed on Saturday.", "text_uk": "", "text_fr": "",
    }).content.decode()
    assert "4 people" in summary and "counted as a service message" in summary
    assert "French is not written. 1 French reader will get the English version." in summary
    assert "Ukrainian is not written" not in summary  # nobody reads Ukrainian here


def test_quick_message_creates_content_per_language_and_calls_the_pipeline(editor_client, volunteers):
    lst, people, outsider = volunteers
    with mock.patch("delivery.pipeline.send_quick_message", create=True) as send:
        response = editor_client.post(reverse("content:quick_message"), {
            "lists": [lst.public_id], "people": [outsider.public_id], "channel": "telegram",
            "text_en": "The parking lot is closed for repaving on Saturday.",
            "text_uk": "У суботу парковка зачинена.",
            "text_fr": "",
        })
    assert response.status_code == 200 and response.content == b""
    assert "on its way to 4 people" in response["HX-Trigger"]

    quick = QuickMessage.objects.get()
    send.assert_called_once_with(quick)
    assert quick.created_by == editor_client.user and quick.channels == ["telegram"]
    assert quick.message_type == "service" and quick.sent_at is not None
    assert list(quick.lists.all()) == [lst] and list(quick.people.all()) == [outsider]
    contents = {c.language: c for c in quick.contents.all()}
    assert set(contents) == {"en", "uk"}  # French is not written, so it has no row and falls back
    assert all(c.form == "notification" for c in contents.values())
    assert contents["uk"].text == "У суботу парковка зачинена."


def test_quick_message_to_a_campaign_list_is_a_newsletter_and_needs_the_default_language(editor_client, volunteers):
    lst, people, outsider = volunteers
    news = List.objects.create(name={"en": "Newsletter"}, slug="newsletter", kind="campaign")
    Subscription.objects.create(person=outsider, list=news, channel="email", state="on")
    with mock.patch("delivery.pipeline.send_quick_message", create=True) as send:
        refused = editor_client.post(reverse("content:quick_message"), {"lists": [news.public_id], "text_uk": "Привіт"})
        assert not send.called and not QuickMessage.objects.exists()
        html = refused.content.decode()
        assert "Write the message in English first" in html and "<dialog" in html and "Newsletter · 1" in html

        nobody = editor_client.post(reverse("content:quick_message"), {"text_en": "Hello"})
        assert "Add at least one person or list" in nobody.content.decode()

        editor_client.post(reverse("content:quick_message"), {"lists": [news.public_id, lst.public_id], "text_en": "Hello"})
    quick = QuickMessage.objects.get()
    assert quick.message_type == "newsletter" and quick.channels == []
    send.assert_called_once_with(quick)
