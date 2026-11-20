"""The wording readers see ships in English, Ukrainian and French (spec section 5)."""
import pytest
from django.test import Client

from content.models import Content
from content.render import build_context, render_email
from core.models import Organization, TeamMember
from core.views import save_languages
from people.models import List

pytestmark = [pytest.mark.django_db, pytest.mark.real_translations]


@pytest.fixture
def org():
    save_languages(["en", "uk", "fr"], "en")
    TeamMember.objects.create_user("owner@example.com", "long password", role="owner")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.setup_complete = True
    org.save()
    List.objects.create(slug="newsletter", kind="campaign", name={"en": "Newsletter", "uk": "Розсилка"}, on_signup_form=True)
    return org


def footer(org, code):
    content = Content(language=code, form="email", subject="News", blocks=[{"id": "b1", "type": "text", "text": "Hello"}])
    ctx = build_context(None, {}, {"unsubscribe": "https://po.example/u/1", "preferences": "https://po.example/p/1"})
    return render_email(content, ctx, message_type="newsletter", org=org).html


def test_the_newsletter_footer_is_written_in_the_readers_language(org):
    assert "Unsubscribe" in footer(org, "en") and "Manage preferences" in footer(org, "en")
    assert "Відписатися" in footer(org, "uk") and "Керувати налаштуваннями" in footer(org, "uk")
    assert "Se désabonner" in footer(org, "fr") and "Gérer mes préférences" in footer(org, "fr")


def test_the_signup_form_follows_the_chosen_language(org):
    c = Client()
    assert "Stay in the loop" in c.get("/join?lang=en").content.decode()
    uk = c.get("/join?lang=uk").content.decode()
    assert "Будьте в курсі" in uk and "Підписатися" in uk and "Розсилка" in uk
    assert "Restez informé" in c.get("/join?lang=fr").content.decode()


def test_a_ukrainian_reader_does_not_turn_the_staff_screens_ukrainian(org):
    c = Client()
    c.get("/join?lang=uk")
    c.post("/sign-in/", {"email": "owner@example.com", "password": "long password"})
    page = c.get("/people/", HTTP_ACCEPT_LANGUAGE="uk").content.decode()
    assert "People" in page and '<html lang="en">' in page
