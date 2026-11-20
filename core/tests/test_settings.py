"""The Settings screen (spec section 13): owner only, six sections saved on their own."""
import re
import sys
import types
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client

from core.models import ApiKey, Building, Language, Organization, TeamMember, hash_key
from core.views import save_languages
from people.models import Person

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh(settings):
    cache.clear()
    settings.PUBLIC_URL = "https://post.example"


@pytest.fixture
def owner():
    save_languages(["en", "uk", "fr"], "en")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.setup_complete = True
    org.save()
    return TeamMember.objects.create_user("andrew@maplegrove.example", "long password", role="owner", name="Andrew")


@pytest.fixture
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def signed_in(member):
    c = Client()
    c.force_login(member)
    return c


# --------------------------------------------------------------------------- who may open it

def test_a_viewer_and_an_editor_cannot_open_settings(owner):
    for role in ("viewer", "editor"):
        c = signed_in(TeamMember.objects.create_user(f"{role}@maplegrove.example", "long password", role=role))
        assert c.get("/settings/").status_code == 403
        assert c.post("/settings/organisation/", {"name": "Taken over", "timezone": "UTC"}).status_code == 403
        assert c.post("/settings/team/invite/", {"email": "friend@example.com", "role": "owner"}).status_code == 403
        assert c.post("/settings/keys/create/", {"name": "Mine", "scope": "all"}).status_code == 403
        assert c.post("/settings/languages/uk/remove/").status_code == 403
        assert c.post(f"/settings/team/tm_{owner.pk}/remove/").status_code == 403
    assert Organization.get().name == "Maple Grove Community Centre"
    assert TeamMember.objects.count() == 3 and not ApiKey.objects.exists() and Language.objects.count() == 3
    assert Client().get("/settings/").status_code == 302


def test_the_page_shows_every_section(client):
    page = client.get("/settings/")
    text = page.content.decode()
    assert page.status_code == 200
    for anchor in ("st-org", "st-lang", "st-bld", "st-cons", "st-team", "st-keys"):
        assert f'id="{anchor}"' in text and f'href="#{anchor}"' in text
    assert "Gatehouse is not available yet. Team members sign in with their email and a password." in text
    assert "Switchboard" not in text


# --------------------------------------------------------------------------- organisation

def test_organisation_is_saved_and_checked(client):
    bad = client.post("/settings/organisation/", {"name": "", "reply_to": "not-an-address", "timezone": "Mars/Olympus"})
    assert bad.status_code == 422 and Organization.get().name == "Maple Grove Community Centre"
    good = client.post("/settings/organisation/", {
        "name": "Maple Grove", "reply_to": "hello@maplegrove.example",
        "mailing_address": "1200 Alder Street, Vancouver, BC", "timezone": "America/Toronto",
    })
    assert good.status_code == 302 and good["Location"] == "/settings/#st-org"
    org = Organization.get()
    assert (org.name, org.reply_to, org.mailing_address, org.timezone) == (
        "Maple Grove", "hello@maplegrove.example", "1200 Alder Street, Vancouver, BC", "America/Toronto")


# --------------------------------------------------------------------------- languages

def test_removing_a_language_moves_its_people_to_the_default(client):
    uk = Language.objects.get(code="uk")
    taras = Person.objects.create(email="taras@example.com", first_name="Taras", language=uk)
    olena = Person.objects.create(email="olena@example.com", first_name="Olena", language=uk)
    marie = Person.objects.create(email="marie@example.com", language=Language.objects.get(code="fr"))
    assert "2 people" in client.get("/settings/").content.decode()

    response = client.post("/settings/languages/uk/remove/")

    assert response.status_code == 302 and Language.codes() == ["en", "fr"]
    for person in (taras, olena):
        person.refresh_from_db()
        assert person.language.code == "en"
    marie.refresh_from_db()
    assert marie.language.code == "fr"


def test_the_default_language_cannot_be_removed(client):
    response = client.post("/settings/languages/en/remove/", follow=True)
    assert Language.codes() == ["en", "uk", "fr"]
    assert "default language cannot be removed" in response.content.decode()
    # Make another one the default, and then it can go.
    client.post("/settings/languages/fr/default/")
    assert Language.default_code() == "fr" and Language.objects.filter(is_default=True).count() == 1
    client.post("/settings/languages/en/remove/")
    assert Language.codes() == ["uk", "fr"]


def test_adding_a_language_without_shipped_wording_warns(client):
    client.post("/settings/languages/add/", {"code": "pl"})
    client.post("/settings/languages/add/", {"code": "pl"})  # twice does nothing
    client.post("/settings/languages/add/", {"code": "xx"})  # not a known language
    assert Language.codes() == ["en", "uk", "fr", "pl"]
    text = client.get("/settings/").content.decode()
    assert "Polski: shown in English" in text and "Українська: complete" in text
    assert "no built-in translation for Polski" in text


def test_missing_translation_choice_is_saved(client):
    client.post("/settings/languages/missing/", {"missing_translation": "hold"})
    assert Organization.get().missing_translation == "hold"
    client.post("/settings/languages/missing/", {"missing_translation": "nonsense"})
    assert Organization.get().missing_translation == "hold"


# --------------------------------------------------------------------------- buildings

def test_connecting_a_building_shows_its_key_once(client):
    response = client.post("/settings/buildings/connect/", {"name": "Town Hall", "kind": "town_hall",
                                                            "base_url": "townhall.maplegrove.example"})
    building = Building.objects.get()
    key = re.search(r'value="(po_bld_[^"]+)"', response.content.decode()).group(1)
    assert building.key_hash == hash_key(key) and building.base_url == "https://townhall.maplegrove.example"
    page = client.get("/settings/").content.decode()
    assert key not in page and "Town Hall" in page and "townhall.maplegrove.example" in page

    rotated = client.post(f"/settings/buildings/{building.public_id}/rotate/")
    new_key = re.search(r'value="(po_bld_[^"]+)"', rotated.content.decode()).group(1)
    building.refresh_from_db()
    assert new_key != key and building.key_hash == hash_key(new_key)

    client.post(f"/settings/buildings/{building.public_id}/disconnect/")
    building.refresh_from_db()
    assert building.key_hash == "" and "Not connected" in client.get("/settings/").content.decode()
    client.post(f"/settings/buildings/{building.public_id}/remove/")
    assert not Building.objects.exists()


def test_a_connected_building_cannot_be_removed_directly(client):
    building = Building.objects.create(name="Pavilion", kind="pavilion")
    building.issue_key()
    client.post(f"/settings/buildings/{building.public_id}/remove/")
    assert Building.objects.filter(pk=building.pk).exists()
    assert client.post("/settings/buildings/connect/", {"name": ""}).status_code == 422


# --------------------------------------------------------------------------- consent

def test_consent_switches_are_saved(client):
    client.post("/settings/consent/", {"keep_consent_proof": "on"})
    org = Organization.get()
    assert (org.confirm_by_email, org.keep_consent_proof, org.tracking_enabled) == (False, True, False)
    client.post("/settings/consent/", {"confirm_by_email": "on", "keep_consent_proof": "on", "tracking_enabled": "on"})
    org = Organization.get()
    assert (org.confirm_by_email, org.keep_consent_proof, org.tracking_enabled) == (True, True, True)
    assert "Always on. Newsletters cannot be sent without it." in client.get("/settings/").content.decode()


# --------------------------------------------------------------------------- team

def test_inviting_someone_creates_a_pending_member_and_shows_the_link(client):
    with mock.patch.dict(sys.modules, {"delivery.pipeline": None}):  # the pipeline is missing: copy the link instead
        response = client.post("/settings/team/invite/", {"email": "Halyna@MapleGrove.example", "role": "editor"})
    member = TeamMember.objects.get(email="halyna@maplegrove.example")
    assert member.role == "editor" and member.invite_token and member.invited_at and not member.has_usable_password()
    text = response.content.decode()
    assert f"https://post.example/invite/{member.invite_token}/" in text
    assert "could not be emailed" in text
    # The invite link works with the page built in core.views.
    assert Client().get(f"/invite/{member.invite_token}/").status_code == 200
    # Inviting the same address again is refused.
    again = client.post("/settings/team/invite/", {"email": "halyna@maplegrove.example", "role": "viewer"})
    assert again.status_code == 422 and TeamMember.objects.count() == 2
    assert client.post("/settings/team/invite/", {"email": "nope", "role": "viewer"}).status_code == 422


def test_the_invite_is_emailed_when_the_pipeline_can(client):
    calls = []

    def send_system_email(to_email, subject, html, text="", person=None):
        calls.append((to_email, subject, html, text))
        return object()

    pipeline = types.ModuleType("delivery.pipeline")
    pipeline.send_system_email = send_system_email
    with mock.patch.dict(sys.modules, {"delivery.pipeline": pipeline}):
        response = client.post("/settings/team/invite/", {"email": "ravi@maplegrove.example", "role": "viewer"})
    member = TeamMember.objects.get(email="ravi@maplegrove.example")
    assert "on its way to ravi@maplegrove.example" in response.content.decode()
    (to_email, subject, html, text), = calls
    link = f"https://post.example/invite/{member.invite_token}/"
    assert to_email == "ravi@maplegrove.example" and "Maple Grove" in subject and link in html and link in text

    pipeline.send_system_email = lambda *a, **k: None  # nothing to send from: not emailed
    with mock.patch.dict(sys.modules, {"delivery.pipeline": pipeline}):
        response = client.post("/settings/team/invite/", {"email": "priya@maplegrove.example", "role": "viewer"})
    assert "could not be emailed" in response.content.decode()


def test_the_last_owner_cannot_be_removed_or_demoted(client, owner):
    editor = TeamMember.objects.create_user("halyna@maplegrove.example", "long password", role="editor", name="Halyna M.")
    # An invited owner who has not joined yet does not count as a second owner.
    TeamMember.objects.create_user("pending@maplegrove.example", None, role="owner", invite_token="tok123")

    response = client.post(f"/settings/team/tm_{owner.pk}/remove/", follow=True)
    assert TeamMember.objects.filter(pk=owner.pk).exists()
    assert "only owner" in response.content.decode()
    response = client.post(f"/settings/team/tm_{owner.pk}/role/", {"role": "editor"}, follow=True)
    owner.refresh_from_db()
    assert owner.role == "owner" and "only owner" in response.content.decode()

    # With a second owner, either may step down.
    client.post(f"/settings/team/tm_{editor.pk}/role/", {"role": "owner"})
    editor.refresh_from_db()
    assert editor.role == "owner"
    client.post(f"/settings/team/tm_{owner.pk}/role/", {"role": "viewer"})
    owner.refresh_from_db()
    assert owner.role == "viewer"
    # ... and the one who stepped down can no longer open Settings.
    assert client.get("/settings/").status_code == 403
    # The remaining owner is now the last one.
    other = signed_in(editor)
    other.post(f"/settings/team/tm_{editor.pk}/remove/")
    assert TeamMember.objects.filter(pk=editor.pk).exists()


def test_changing_a_role_and_removing_a_member(client, owner):
    ravi = TeamMember.objects.create_user("ravi@maplegrove.example", "long password", role="viewer", name="Ravi B.")
    client.post(f"/settings/team/tm_{ravi.pk}/role/", {"role": "editor"})
    ravi.refresh_from_db()
    assert ravi.role == "editor"
    client.post(f"/settings/team/tm_{ravi.pk}/role/", {"role": "emperor"})
    ravi.refresh_from_db()
    assert ravi.role == "editor"
    client.post(f"/settings/team/tm_{ravi.pk}/remove/")
    assert not TeamMember.objects.filter(pk=ravi.pk).exists()
    assert client.post("/settings/team/tm_999999/remove/").status_code == 404
    assert client.post(f"/settings/team/{owner.pk}/remove/").status_code == 404  # raw database keys are not accepted


# --------------------------------------------------------------------------- API keys

def test_a_new_api_key_is_shown_once_and_stored_only_as_a_hash(client):
    response = client.post("/settings/keys/create/", {"name": "Website signup form", "scope": "subscribe"})
    text = response.content.decode()
    key = re.search(r'value="(po_key_[^"]+)"', text).group(1)
    api_key = ApiKey.objects.get()
    assert (api_key.name, api_key.scope) == ("Website signup form", "subscribe")
    assert api_key.key_hash == hash_key(key) and len(api_key.key_hash) == 64
    # Nothing in the row holds the key itself.
    assert key not in " ".join(str(v) for v in ApiKey.objects.values().get().values())
    assert api_key.key_prefix == key[:14] and len(key) > 40
    # Shown once: the page no longer has it, only the short prefix.
    page = client.get("/settings/").content.decode()
    assert key not in page and api_key.key_prefix in page and "Website signup form" in page
    assert key not in str(dict(client.session))

    assert client.post("/settings/keys/create/", {"name": "", "scope": "send"}).status_code == 422
    client.post(f"/settings/keys/{api_key.public_id}/revoke/")
    api_key.refresh_from_db()
    assert api_key.revoked_at is not None and not api_key.allows("subscribe")
