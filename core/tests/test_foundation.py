import pytest
from django.test import Client

from content.models import Content
from content.render import TemplateNotAllowed, build_context, render_email, render_jinja, render_notification, validate_jinja
from core.models import Language, Organization, TeamMember
from core.views import save_languages

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_first_run(settings):
    # FirstRunMiddleware caches "an owner exists" per process; tests start from scratch.
    from django.core.cache import cache

    cache.clear()


def test_first_visit_leads_to_setup():
    response = Client().get("/")
    assert response.status_code == 302 and response["Location"] == "/setup/"
    assert Client().get("/setup/").status_code == 200


def test_setup_creates_an_owner_and_three_languages():
    c = Client()
    r = c.post("/setup/", {
        "org_name": "Maple Grove Community Centre", "name": "Andrew", "email": "andrew@example.com",
        "password": "correct horse", "timezone": "America/Vancouver", "mailing_address": "",
    })
    assert r.status_code == 302
    owner = TeamMember.objects.get()
    assert owner.role == "owner" and owner.check_password("correct horse")
    r = c.post("/setup/2/", {"languages": ["en", "uk", "fr"], "default": "en"})
    assert r.status_code == 302
    assert list(Language.objects.values_list("code", flat=True)) == ["en", "uk", "fr"]
    assert Language.default_code() == "en"
    assert Organization.get().name == "Maple Grove Community Centre"


def test_sign_in_with_email_and_password():
    TeamMember.objects.create_user("owner@example.com", "long password", role="owner", name="Olena")
    org = Organization.get()
    org.setup_complete = True
    org.save()
    c = Client()
    assert c.get("/sign-in/").status_code == 200
    bad = c.post("/sign-in/", {"email": "owner@example.com", "password": "nope"})
    assert bad.status_code == 200 and b"do not match" in bad.content
    good = c.post("/sign-in/", {"email": "Owner@Example.com", "password": "long password"})
    assert good.status_code == 302 and good["Location"] == "/"
    assert c.get("/").status_code == 200


def test_a_viewer_cannot_open_settings():
    TeamMember.objects.create_user("owner@example.com", "long password", role="owner")
    TeamMember.objects.create_user("viewer@example.com", "long password", role="viewer")
    c = Client()
    c.post("/sign-in/", {"email": "viewer@example.com", "password": "long password"})
    assert c.get("/").status_code == 200
    assert c.get("/settings/").status_code == 403


def test_email_renders_blocks_placeholders_and_footer():
    save_languages(["en", "uk"], "en")
    org = Organization.get()
    org.name = "Maple Grove"
    org.mailing_address = "12 Alder Street"
    org.save()
    content = Content(language="en", form="email", subject="Hello {first_name}", blocks=[
        {"id": "b1", "type": "heading", "text": "December"},
        {"id": "b2", "type": "text", "text": "Hi {first_name},\nsee *this* and [the calendar](https://example.org/c)."},
        {"id": "b3", "type": "button", "text": "Open", "href": "https://example.org/x"},
    ])
    ctx = build_context(None, {}, {"unsubscribe": "https://po/u/1", "preferences": "https://po/p/1"})
    out = render_email(content, ctx, message_type="newsletter", org=org)
    assert out.subject == "Hello"  # the empty name swallowed its space
    assert "Hi,<br>see <strong>this</strong>" in out.html
    assert 'href="https://example.org/c"' in out.html and "Unsubscribe" in out.html and "12 Alder Street" in out.html
    service = render_email(content, ctx, message_type="service", org=org)
    assert "Unsubscribe" not in service.html
    tracked = render_email(content, ctx, org=org, track=lambda u: "https://po/t/c/x?u=" + u)
    assert "https://po/t/c/x?u=https://example.org/x" in tracked.html


def test_html_mode_allows_only_variables_and_loops():
    assert render_jinja("{{ order.number }} {% for i in order.items %}[{{ i.name }}]{% endfor %}",
                        {"order": {"number": "#1", "items": [{"name": "<b>"}]}}) == "#1 [&lt;b&gt;]"
    warnings = []
    assert render_jinja("a{{ nope.deep }}b", {}, warnings) == "ab" and warnings
    for bad in ("{% if x %}y{% endif %}", "{{ x|upper }}", "{{ x.__class__ }}", "{{ f() }}", "{% set a = 1 %}"):
        with pytest.raises(TemplateNotAllowed):
            validate_jinja(bad)


def test_notification_for_telegram_and_push():
    content = Content(language="en", form="notification", text="*Closed* on Saturday.\nUse [Alder](https://x.org) " + "x" * 200)
    out = render_notification(content, build_context(None))
    assert out.telegram_html.startswith('<b>Closed</b> on Saturday.\nUse <a href="https://x.org">Alder</a>')
    assert out.push_body.startswith("Closed on Saturday. Use Alder") and len(out.push_body) <= 150
