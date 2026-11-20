"""Fixtures shared by the people and public tests: the fictional Maple Grove Community Centre."""
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client

from core.models import Building, Organization, TeamMember
from core.views import save_languages
from people.models import List


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()  # rate limits and Telegram connect tokens live in the cache
    yield
    cache.clear()


@pytest.fixture
def org(db):
    save_languages(["en", "uk", "fr"], "en")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.mailing_address = "12 Alder Street, Maple Grove"
    org.setup_complete = True
    org.confirm_by_email = True
    org.save()
    TeamMember.objects.create_user("owner@maplegrove.example", "long password", role="owner", name="Andrew")
    return org


@pytest.fixture
def editor(org):
    return TeamMember.objects.create_user("editor@maplegrove.example", "long password", role="editor", name="Priya")


@pytest.fixture
def viewer(org):
    return TeamMember.objects.create_user("viewer@maplegrove.example", "long password", role="viewer", name="Marc")


@pytest.fixture
def editor_client(editor):
    client = Client()
    client.force_login(editor)
    return client


@pytest.fixture
def viewer_client(viewer):
    client = Client()
    client.force_login(viewer)
    return client


@pytest.fixture
def lists(org):
    """Two campaign lists on the signup form, one that is not, and Town Hall's app list."""
    town_hall = Building.objects.create(name="Town Hall", kind="town_hall")
    return {
        "newsletter": List.objects.create(name={"en": "Monthly newsletter", "uk": "Щомісячна розсилка"}, slug="newsletter",
                                          kind="campaign", on_signup_form=True, position=1),
        "events": List.objects.create(name={"en": "Event updates"}, slug="events", kind="campaign", on_signup_form=True, position=2),
        "pavilion": List.objects.create(name={"en": "Pavilion news"}, slug="pavilion-news", kind="campaign", position=3),
        "volunteers": List.objects.create(name={"en": "Volunteers"}, slug="volunteers", kind="app", building=town_hall, position=4),
    }


@pytest.fixture
def sent():
    """Every system email that would have been sent: a list of (to_email, subject, html, text, person)."""
    calls = []

    def fake(to_email, subject, html, text="", person=None):
        calls.append({"to": to_email, "subject": subject, "html": html, "text": text, "person": person})
        return mock.Mock(state="sent")

    with mock.patch("delivery.pipeline.send_system_email", side_effect=fake, create=True):
        yield calls
