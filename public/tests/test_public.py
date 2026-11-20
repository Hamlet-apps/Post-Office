"""Step 5 of the build: the signup form, pre-filled links, confirmation email, preferences and unsubscribe."""
import re

import pytest
from django.test import Client

from core.models import Organization
from people import services
from people.models import ConsentRecord, Person, Subscription
from people.signup import SignupResult, consent_wording, process_signup
from people.tokens import preferences_token

pytestmark = pytest.mark.django_db

WORDING = "I agree to receive these messages. I can unsubscribe at any time."
BUTTON_WORDING = "By pressing Subscribe, I agree to receive these messages. I can unsubscribe at any time."
FORM = {"name": "Olena", "email": "olena.k@example.com", "lists": ["newsletter", "events"], "consent": "1", "lang": "uk"}


def _consent_box(html):
    """The consent tick box's <input> tag, or None when the form has none."""
    match = re.search(r'<input[^>]*name="consent"[^>]*>', html)
    return match.group(0) if match else None


def _without_tokens(html):
    """A page with the per-request CSRF values and single-use Telegram tokens removed, for comparing."""
    html = re.sub(r'(X-CSRFToken": "|csrfmiddlewaretoken" value=")[A-Za-z0-9]+', r"\1", html)
    return re.sub(r"\?start=[A-Za-z0-9_-]+", "?start=", html)


def _subscribe(person, lst, channel="email"):
    consent = services.record_consent(person, [lst], "signup_form", WORDING)
    return services.set_subscription(person, lst, channel, True, consent=consent)


@pytest.fixture
def bot(org):
    from channels.models import TelegramBot

    return TelegramBot.objects.create(name="Maple Grove", username="maplegrove_bot", token="123:abc", is_default=True)


# --------------------------------------------------------------------------- the form

def test_the_form_shows_the_lists_on_it_and_an_unticked_consent_box(client, lists, bot):
    response = client.get("/join", HTTP_ACCEPT_LANGUAGE="fr-CA,fr;q=0.9,en;q=0.5")
    assert response.status_code == 200
    html = response.content.decode()
    assert response.context["code"] == "fr" and '<main class="pub-page" lang="fr">' in html  # the browser's language
    assert "Maple Grove Community Centre" in html
    assert 'value="newsletter"' in html and 'value="events"' in html
    assert "pavilion-news" not in html and "volunteers" not in html  # not on the form; app lists never are
    assert 'name="telegram"' in html and 'name="website"' in html
    assert "checked" not in _consent_box(html)
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"

    assert client.get("/join?lang=uk", HTTP_ACCEPT_LANGUAGE="fr").context["code"] == "uk"  # ?lang= wins
    assert client.get("/join", HTTP_ACCEPT_LANGUAGE="de").context["code"] == "en"  # unsupported: the default


def test_a_prefilled_link_fills_every_field_but_never_gives_consent(client, lists, bot):
    link = "/join?name=Olena&email=olena.k%40example.com&lists=newsletter,events&telegram=1&lang=uk&consent=1&agreed=1"
    response = client.get(link)
    html = response.content.decode()
    assert 'name="name" value="Olena"' in html
    assert 'name="email" value="olena.k@example.com"' in html
    assert 'name="lists" value="newsletter" checked' in html and 'name="lists" value="events" checked' in html
    assert 'name="telegram" value="1" checked' in html
    assert response.context["code"] == "uk" and 'lang="uk"' in html
    # No consent box to tick, let alone a ticked one: the wording sits beside the button instead.
    assert _consent_box(html) is None
    assert BUTTON_WORDING in html
    # Opening the link saved nothing.
    assert Person.objects.count() == 0 and ConsentRecord.objects.count() == 0

    # Without anything pre-filled the box is there, and no query parameter can tick it.
    for url in ("/join?consent=1", "/join?consent=on&lang=uk", "/join?lang=uk"):
        box = _consent_box(client.get(url).content.decode())
        assert box is not None and "checked" not in box


def test_the_consent_box_stays_unticked_after_an_error(client, lists, sent):
    response = client.post("/join", {**FORM, "email": "not an address"})
    html = response.content.decode()
    assert "Enter your email address" in html and 'value="Olena"' in html
    assert "checked" not in _consent_box(html)
    assert Person.objects.count() == 0 and sent == []


def test_subscribing_without_the_tick_is_refused(client, lists, sent):
    data = {k: v for k, v in FORM.items() if k != "consent"}
    response = client.post("/join", data)
    assert b"Tick the box to say you agree" in response.content
    # A consent value in the address is not consent either.
    response = client.post("/join?consent=1&lang=uk", data)
    assert b"Tick the box to say you agree" in response.content
    assert Person.objects.count() == 0 and ConsentRecord.objects.count() == 0 and sent == []

    response = client.post("/join", {**FORM, "lists": []})
    assert b"Tick at least one thing" in response.content
    assert Person.objects.count() == 0


def test_subscribing_records_the_person_their_lists_and_the_proof(client, lists, bot, sent):
    response = client.post("/join", {**FORM, "telegram": "1"}, HTTP_USER_AGENT="Firefox on a phone", REMOTE_ADDR="203.0.113.9")
    assert response.status_code == 200 and b"Thank you" in response.content
    person = Person.objects.get(email="olena.k@example.com")
    assert (person.first_name, person.language.code, person.source) == ("Olena", "uk", "signup_form")
    consent = ConsentRecord.objects.get(person=person)
    assert consent.method == "signup_form"
    assert consent.wording == consent_wording("uk") == WORDING  # the exact text shown (English until the catalogue is filled)
    assert (consent.language, consent.ip, consent.user_agent) == ("uk", "203.0.113.9", "Firefox on a phone")
    assert consent.link_params == {} and consent.confirmed_at is None
    assert {l.slug for l in consent.lists.all()} == {"newsletter", "events"}
    states = {(s.list.slug, s.channel): s.state for s in person.subscriptions.select_related("list")}
    assert states == {("newsletter", "email"): "unconfirmed", ("events", "email"): "unconfirmed",
                      ("newsletter", "telegram"): "on", ("events", "telegram"): "on"}
    # The thank-you page offers the Telegram connect link because the box was ticked.
    assert b"https://t.me/maplegrove_bot?start=" in response.content


def test_a_prefilled_link_subscribes_with_the_button_and_records_that_wording(client, lists, sent):
    link = "/join?name=Olena&email=olena.k%40example.com&lists=newsletter&lang=uk"
    response = client.post(link, {"name": "Olena", "email": "olena.k@example.com", "lists": ["newsletter"], "lang": "uk"})
    assert b"Thank you" in response.content
    consent = ConsentRecord.objects.get()
    assert consent.wording == BUTTON_WORDING
    assert consent.link_params == {"name": "Olena", "email": "olena.k@example.com", "lists": "newsletter", "lang": "uk"}
    assert b"t.me" not in response.content  # the Telegram box was not ticked


def test_an_unconfirmed_subscriber_is_not_on_until_they_confirm(client, lists, sent):
    client.post("/join", FORM)
    person = Person.objects.get(email="olena.k@example.com")
    subs = Subscription.objects.filter(person=person, channel="email")
    assert {s.state for s in subs} == {"unconfirmed"}
    # Unconfirmed people are not on the list, so a campaign to it would not reach them.
    assert person not in lists["newsletter"].people()

    # Exactly one email, to them, with the confirm button.
    assert len(sent) == 1 and sent[0]["to"] == "olena.k@example.com" and sent[0]["person"] == person
    link = re.search(r'href="[^"]*(/confirm/[^"]+)"', sent[0]["html"]).group(1)
    assert link in sent[0]["text"]
    assert "Maple Grove Community Centre" in sent[0]["html"] and "Щомісячна розсилка" in sent[0]["html"]  # in their language

    assert client.get("/confirm/not-a-real-token").status_code == 404
    assert {s.state for s in subs.all()} == {"unconfirmed"}

    response = client.get(link)
    assert response.status_code == 200 and b"confirmed" in response.content
    assert {s.state for s in subs.all()} == {"on"}
    assert ConsentRecord.objects.get(person=person).confirmed_at is not None
    assert person in lists["newsletter"].people()


def test_without_confirm_by_email_subscriptions_start_on(client, lists, sent):
    Organization.objects.update(confirm_by_email=False)
    client.post("/join", FORM)
    assert {s.state for s in Subscription.objects.all()} == {"on"}
    assert sent == []


def test_an_existing_address_does_not_reveal_itself(client, lists, bot, sent):
    james = Person.objects.create(email="james.whitfield@example.com", first_name="James")
    _subscribe(james, lists["events"])
    data = {**FORM, "name": "Mallory", "lists": ["newsletter"], "telegram": "1", "lang": "en"}

    new = client.post("/join", {**data, "email": "someone.new@example.com"})
    existing = client.post("/join", {**data, "email": "James.Whitfield@example.com"})
    assert new.status_code == existing.status_code == 200
    assert _without_tokens(existing.content.decode()) == _without_tokens(new.content.decode())

    # Nothing about James changed, and nobody can attach their Telegram to him from the thank-you page.
    james.refresh_from_db()
    assert james.first_name == "James"
    assert {(s.list.slug, s.channel, s.state) for s in james.subscriptions.select_related("list")} == {("events", "email", "on")}
    assert ConsentRecord.objects.filter(person=james).count() == 1
    from people.tokens import person_from_connect_token

    token = re.search(r"\?start=([A-Za-z0-9_-]+)", existing.content.decode()).group(1)
    assert person_from_connect_token(token) is None

    # Instead, James gets an email with a link to his preferences; the new person gets the confirmation.
    assert [m["to"] for m in sent] == ["someone.new@example.com", "james.whitfield@example.com"]
    assert "/confirm/" in sent[0]["html"]
    assert f"/p/{preferences_token(james)}" in sent[1]["html"] and "/confirm/" not in sent[1]["html"]
    assert sent[1]["person"] == james


def test_process_signup_keeps_the_signature_the_api_calls(lists, sent):
    result = process_signup(email="Priya.Nair@example.com", first_name="Priya", language_code="en", list_slugs=["newsletter", "volunteers", "nope"],
                            telegram=False, wording="Send me the newsletter", ip="198.51.100.4", user_agent="curl", link_params=None, method="api")
    assert isinstance(result, SignupResult) and result.existing is False
    assert result.person.email == "priya.nair@example.com" and result.person.source == "api"
    assert result.consent.method == "api" and result.consent.wording == "Send me the newsletter"
    # Only campaign lists can be joined this way; app lists and unknown slugs are ignored.
    assert [(s.list.slug, s.channel, s.state) for s in result.person.subscriptions.select_related("list")] == [("newsletter", "email", "unconfirmed")]

    again = process_signup(email="priya.nair@example.com", list_slugs=["events"], wording="Send me the newsletter")
    assert again.existing is True and again.consent is None and again.person == result.person
    assert result.person.subscriptions.count() == 1


def test_a_first_name_cannot_put_a_link_into_the_confirmation_email(lists, sent):
    process_signup(email="olena.k@example.com", first_name="[Claim your prize](https://bad.example) {preferences_link}",
                   list_slugs=["newsletter"], wording=WORDING)
    assert "bad.example\"" not in sent[0]["html"] and 'href="https://bad.example' not in sent[0]["html"]
    assert "/p/" not in sent[0]["html"]


def test_the_honeypot_and_the_rate_limit(client, lists, sent):
    response = client.post("/join", {**FORM, "website": "https://spam.example"})
    assert response.status_code == 200 and b"Thank you" in response.content
    assert Person.objects.count() == 0 and sent == []

    for n in range(10):
        assert client.post("/join", {**FORM, "email": f"person{n}@example.com"}, REMOTE_ADDR="203.0.113.50").status_code == 200
    blocked = client.post("/join", {**FORM, "email": "one.too.many@example.com"}, REMOTE_ADDR="203.0.113.50")
    assert blocked.status_code == 429 and b"Too many signups" in blocked.content
    assert Person.objects.count() == 10
    # Another address is not affected.
    assert client.post("/join", {**FORM, "email": "elsewhere@example.com"}, REMOTE_ADDR="203.0.113.51").status_code == 200


def test_switching_language_keeps_what_was_typed_and_saves_nothing(client, lists):
    response = client.post("/join", {"name": "Olena", "email": "olena.k@example.com", "lists": ["events"], "lang": "en", "switch_lang": "uk"})
    html = response.content.decode()
    assert response.context["code"] == "uk" and 'value="Olena"' in html and 'value="events" checked' in html
    assert "checked" not in _consent_box(html)
    assert Person.objects.count() == 0


def test_the_staff_preview_tries_out_unsaved_switches_and_cannot_submit(editor_client, client, lists):
    url = "/join?preview=1&pv_lists=pavilion-news&pv_first_name=0&pv_language=0&pv_telegram=0"
    html = editor_client.get(url).content.decode()
    assert 'value="pavilion-news"' in html and 'value="newsletter"' not in html and 'name="name"' not in html
    assert "This is a preview" in html
    editor_client.post(url, {"email": "olena.k@example.com", "lists": ["pavilion-news"], "consent": "1"})
    assert Person.objects.count() == 0
    # Someone who is not signed in gets the real form whatever the address says.
    html = client.get(url).content.decode()
    assert 'value="newsletter"' in html and 'value="pavilion-news"' not in html and "This is a preview" not in html


# --------------------------------------------------------------------------- preferences

def _prefs(person):
    return f"/p/{preferences_token(person)}"


def test_the_preferences_page_shows_the_grid_in_the_persons_language(client, lists):
    person = Person.objects.create(email="olena.k@example.com", first_name="Olena")
    services.find_or_create_person(email=person.email)
    person.language_id = person.language.__class__.objects.get(code="uk").pk
    person.save()
    _subscribe(person, lists["newsletter"])
    services.set_subscription(person, lists["volunteers"], "email", True)

    response = client.get(_prefs(person))
    assert response.status_code == 200
    html = response.content.decode()
    assert '<main class="pub-page" lang="uk">' in html
    assert "Щомісячна розсилка" in html  # the list's Ukrainian name
    assert "Olena, choose what you hear about" in html
    assert "News" in html and "About things you signed up for" in html and "Volunteers" in html
    assert "switched on and off in those apps" in html
    assert "Unsubscribe from all news" in html
    grid = response.context["grid"]
    assert [r["list"].slug for r in grid["campaign"]] == ["newsletter", "events", "pavilion-news"]
    assert [r["list"].slug for r in grid["app"]] == ["volunteers"]

    assert client.get("/p/tampered-token").status_code == 404


def test_changes_on_the_preferences_page_save_as_they_are_made(client, lists):
    person = Person.objects.create(email="olena.k@example.com", first_name="Olena")
    _subscribe(person, lists["newsletter"])
    services.set_subscription(person, lists["volunteers"], "email", True)
    url = _prefs(person) + "/toggle"

    response = client.post(url, {"list": lists["newsletter"].public_id, "channel": "email", "on": "0"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and b'id="prefs-grid"' in response.content
    assert Subscription.objects.get(person=person, list=lists["newsletter"], channel="email").state == "off"

    # Switching a newsletter back on is the person's own consent, recorded with what they saw.
    client.post(url, {"list": lists["events"].public_id, "channel": "email", "on": "1"}, HTTP_USER_AGENT="Safari", REMOTE_ADDR="203.0.113.7")
    sub = Subscription.objects.get(person=person, list=lists["events"], channel="email")
    assert sub.state == "on"
    assert sub.consent.method == "signup_form" and sub.consent.ip == "203.0.113.7" and sub.consent.user_agent == "Safari"
    assert "Tick how you want to get each kind of message" in sub.consent.wording and "Event updates by email" in sub.consent.wording

    # App lists can be switched off and on again without consent.
    client.post(url, {"list": lists["volunteers"].public_id, "channel": "email", "on": "0"})
    assert Subscription.objects.get(person=person, list=lists["volunteers"], channel="email").state == "off"
    client.post(url, {"list": lists["volunteers"].public_id, "channel": "email", "on": "1"})
    assert Subscription.objects.get(person=person, list=lists["volunteers"], channel="email").state == "on"

    # A channel the page does not offer cannot be switched.
    assert client.post(url, {"list": lists["volunteers"].public_id, "channel": "push", "on": "1"}).status_code == 400
    assert client.post("/p/tampered/toggle", {"list": lists["events"].public_id, "channel": "email", "on": "0"}).status_code == 404


def test_the_language_switch_changes_the_persons_language(client, lists):
    person = Person.objects.create(email="camille.t@example.com", first_name="Camille")
    assert person.language.code == "en"
    response = client.post(_prefs(person) + "/language", {"lang": "fr"})
    assert response.status_code == 302 and response["Location"] == _prefs(person)
    person.refresh_from_db()
    assert person.language.code == "fr"
    client.post(_prefs(person) + "/language", {"lang": "xx"})
    person.refresh_from_db()
    assert person.language.code == "fr"


def test_unsubscribe_from_all_news_leaves_app_messages(client, lists):
    person = Person.objects.create(email="olena.k@example.com")
    _subscribe(person, lists["newsletter"], "email")
    _subscribe(person, lists["events"], "telegram")
    services.set_subscription(person, lists["volunteers"], "email", True)
    response = client.post(_prefs(person) + "/unsubscribe-all", HTTP_HX_REQUEST="true")
    assert b"unsubscribed from all news" in response.content
    states = {(s.list.slug, s.channel): s.state for s in person.subscriptions.select_related("list")}
    assert states == {("newsletter", "email"): "off", ("events", "telegram"): "off", ("volunteers", "email"): "on"}


# --------------------------------------------------------------------------- one-click unsubscribe

def _message(person, lst=None, campaign=None, key="m:1"):
    from delivery.models import Message

    return Message.objects.create(person=person, source="campaign", message_type="newsletter", channel="email", list=lst,
                                  campaign=campaign, state="sent", idempotency_key=key)


def test_unsubscribe_turns_off_one_list_for_email_only(lists):
    person = Person.objects.create(email="olena.k@example.com", first_name="Olena")
    for lst in (lists["newsletter"], lists["events"]):
        for channel in ("email", "telegram"):
            _subscribe(person, lst, channel)
    message = _message(person, lists["newsletter"])

    response = Client().get(f"/u/{message.token}")
    assert response.status_code == 302 and response["Location"].startswith(f"/p/{preferences_token(person)}")
    states = {(s.list.slug, s.channel): s.state for s in person.subscriptions.select_related("list")}
    assert states == {("newsletter", "email"): "off", ("newsletter", "telegram"): "on",
                      ("events", "email"): "on", ("events", "telegram"): "on"}
    # ... then the preferences page, saying what happened.
    page = Client().get(response["Location"])
    assert page.status_code == 200 and b"You will no longer get Monthly newsletter by email" in page.content

    assert Client().get("/u/no-such-message").status_code == 404


def test_one_click_unsubscribe_by_post_works_without_a_csrf_token(lists):
    person = Person.objects.create(email="olena.k@example.com")
    _subscribe(person, lists["newsletter"], "email")
    _subscribe(person, lists["events"], "email")
    message = _message(person, lists["events"])
    mail_app = Client(enforce_csrf_checks=True)
    response = mail_app.post(f"/u/{message.token}", {"List-Unsubscribe": "One-Click"})
    assert response.status_code == 200
    states = {s.list.slug: s.state for s in person.subscriptions.select_related("list")}
    assert states == {"newsletter": "on", "events": "off"}


def test_unsubscribe_without_a_list_uses_the_campaigns_lists(lists, editor):
    from campaigns.models import Campaign

    person = Person.objects.create(email="olena.k@example.com")
    for lst in (lists["newsletter"], lists["events"], lists["pavilion"]):
        _subscribe(person, lst, "email")
    _subscribe(person, lists["newsletter"], "telegram")
    campaign = Campaign.objects.create(name="Winter coat drive", created_by=editor)
    campaign.lists.set([lists["newsletter"], lists["events"]])
    message = _message(person, campaign=campaign)

    assert Client().get(f"/u/{message.token}").status_code == 302
    states = {(s.list.slug, s.channel): s.state for s in person.subscriptions.select_related("list")}
    assert states == {("newsletter", "email"): "off", ("events", "email"): "off", ("pavilion-news", "email"): "on",
                      ("newsletter", "telegram"): "on"}


def test_the_done_page_and_the_telegram_offer_render(client, lists, bot):
    person = Person.objects.create(email="olena.k@example.com", first_name="Olena")
    page = client.get(_prefs(person))
    assert b"Connect Telegram" in page.content and b"https://t.me/maplegrove_bot?start=" in page.content
    done = client.get(_prefs(person) + "?done=1")
    assert done.status_code == 200 and b"Your choices are saved" in done.content
