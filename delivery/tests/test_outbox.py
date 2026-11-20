import pytest
from django.test import Client

from core.models import TeamMember
from delivery import pipeline
from delivery.models import LinkClick, Message, TrackedLink

from . import helpers as h

pytestmark = pytest.mark.django_db


def _client(role):
    TeamMember.objects.create_user("owner@maplegrove.example", "long password", role="owner", name="Andrew")
    if role != "owner":
        TeamMember.objects.create_user(f"{role}@maplegrove.example", "long password", role=role, name=role.title())
    client = Client()
    assert client.login(email=f"{role}@maplegrove.example", password="long password")
    return client


@pytest.fixture
def sent_campaign(org, smtp):
    newsletter = h.make_list()
    for n in (1, 2, 3):
        h.subscribe(h.make_person(f"person{n}@example.com", f"Person{n}"), newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender())
    pipeline.send_campaign(campaign)
    campaign.refresh_from_db()
    return campaign


def test_open_pixel_and_click_redirect_update_the_message(sent_campaign):
    message = Message.objects.order_by("pk").first()
    client = Client()
    pixel = client.get(f"/t/o/{message.token}.gif")
    assert pixel.status_code == 200 and pixel["Content-Type"] == "image/gif"
    message.refresh_from_db()
    assert message.state == "opened" and message.opened_at is not None

    link = TrackedLink.objects.get(campaign=sent_campaign)
    assert link.url == "https://maplegrove.example/calendar"
    response = client.get(f"/t/c/{link.token}?m={message.token}")
    assert response.status_code == 302 and response["Location"] == "https://maplegrove.example/calendar"
    message.refresh_from_db()
    assert message.state == "clicked" and message.clicked_at is not None
    assert LinkClick.objects.filter(message=message, link=link).count() == 1

    # A click with no open before it counts as an open too.
    other = Message.objects.order_by("pk").last()
    client.get(f"/t/c/{link.token}?m={other.token}")
    other.refresh_from_db()
    assert other.state == "clicked" and other.opened_at is not None
    assert client.get("/t/c/nope").status_code == 404
    assert client.get("/t/o/unknown.gif").status_code == 200


def test_viewer_sees_a_campaign_as_one_summary_row_until_expanded(sent_campaign):
    client = _client("viewer")
    page = client.get("/outbox/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "Winter coat drive" in html and "3 people" in html and "Newsletter" in html
    assert f'href="/campaigns/{sent_campaign.public_id}/"' in html
    assert "person1@example.com" not in html and "Person1" not in html
    rows = client.get(f"/outbox/campaign/{sent_campaign.public_id}/", HTTP_HX_REQUEST="true")
    assert rows.status_code == 200 and rows.content.decode().count("<tr") == 3 and "Person1" in rows.content.decode()


def test_scheduled_campaign_links_to_its_send_screen_and_counts(org):
    newsletter = h.make_list()
    h.subscribe(h.make_person(), newsletter)
    campaign = h.make_campaign([newsletter], sender=h.make_sender(), name="December newsletter")
    html = _client("viewer").get("/outbox/").content.decode()
    assert f'href="/campaigns/{campaign.public_id}/send/"' in html and "1 person" in html and "Scheduled" in html


def test_app_messages_are_listed_one_by_one_with_filters_and_search(org, smtp):
    building, _key = h.make_building()
    template = h.make_template(building, notification=False)
    h.make_sender("app")
    olena = h.make_person()
    marc = h.make_person("marc.gagnon@example.com", "Marc")
    pipeline.send_app_message(template, olena, {"order": {"number": "#0142"}})
    pipeline.send_app_message(template, marc, {"order": {"number": "#0139"}})
    client = _client("viewer")
    html = client.get("/outbox/").content.decode()
    assert html.count('class="ob-message"') == 2 and 'href="/templates/pavilion.order_confirmation/"' in html
    assert "Order #0142 (Olena)" in html and "Pavilion" in html
    assert client.get("/outbox/?q=marc").content.decode().count('class="ob-message"') == 1
    assert client.get(f"/outbox/?source={building.public_id}").content.decode().count('class="ob-message"') == 2
    assert client.get("/outbox/?source=campaigns").content.decode().count('class="ob-message"') == 0
    assert client.get("/outbox/?status=failed").content.decode().count('class="ob-message"') == 0
    assert client.get("/outbox/?channel=telegram").content.decode().count('class="ob-message"') == 0
    assert client.get("/outbox/?from=2001-01-01&to=2001-01-02").content.decode().count('class="ob-message"') == 0


def test_failed_row_shows_the_reason_and_only_editors_can_try_again(org, smtp):
    import smtplib

    building, _key = h.make_building()
    template = h.make_template(building, notification=False)
    h.make_sender("app")
    person = h.make_person()
    smtp.fail_with = smtplib.SMTPDataError(554, b"Message rejected")
    message = pipeline.send_app_message(template, person, {})[0]
    assert message.state == "failed"
    viewer = _client("viewer")
    html = viewer.get("/outbox/").content.decode()
    assert "554 Message rejected" in html and f'href="/people/{person.public_id}/"' in html and "Try again" not in html
    assert viewer.post(f"/outbox/{message.public_id}/retry/").status_code == 403
    editor = Client()
    TeamMember.objects.create_user("editor@maplegrove.example", "long password", role="editor")
    editor.login(email="editor@maplegrove.example", password="long password")
    assert "Try again" in editor.get("/outbox/").content.decode()
    smtp.fail_with = None
    response = editor.post(f"/outbox/{message.public_id}/retry/", HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and "Delivered" in response.content.decode()
    message.refresh_from_db()
    assert message.state == "sent"


def test_outbox_needs_sign_in(org):
    TeamMember.objects.create_user("owner@maplegrove.example", "long password", role="owner")
    response = Client().get("/outbox/")
    assert response.status_code == 302 and "/sign-in/" in response["Location"]
