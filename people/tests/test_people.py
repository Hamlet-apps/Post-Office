"""Step 2 of the build: people, lists, subscriptions, consent, the People and Person screens, import."""
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from core.models import Organization
from people import importer, services
from people.models import ConsentRecord, List, Person, Subscription, Suppression
from people.tasks import delete_unconfirmed_subscriptions

pytestmark = pytest.mark.django_db

CSV = (
    "email,first_name,last_name,language,Neighbourhood\n"
    "olena.k@example.com,Olena,Kovalenko,uk,Maple Ridge\n"
    "James.Whitfield@example.com,James,Whitfield,English,Alder Park\n"
    "not-an-address,Nobody,,en,\n"
    "olena.k@example.com,Olena,Again,uk,\n"
)


def _upload(text=CSV, name="people.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def _person(email="olena.k@example.com", first="Olena", last="Kovalenko", lang="uk"):
    person, _created = services.find_or_create_person(email=email, first_name=first, last_name=last, language_code=lang, source="staff")
    return person


def _subscribe(person, lst, channel="email"):
    consent = services.record_consent(person, [lst], "signup_form", "I agree to receive these messages.")
    return services.set_subscription(person, lst, channel, True, consent=consent)


# --------------------------------------------------------------------------- import

def test_importing_a_csv_without_the_consent_tick_is_refused(editor_client, lists):
    response = editor_client.post("/people/import/", {"file": _upload(), "list": lists["newsletter"].public_id, "action": "preview"})
    assert response.status_code == 400
    assert b"Tick the box" in response.content
    assert Person.objects.count() == 0

    # The importer itself refuses too, whatever the screen does.
    parsed = importer.parse_csv(CSV)
    with pytest.raises(services.ConsentRequired):
        importer.run_import(parsed, lists["newsletter"], consent_given=False)
    assert Person.objects.count() == 0 and ConsentRecord.objects.count() == 0


def test_a_commit_without_the_tick_is_refused_even_after_a_preview(editor_client, lists):
    data = {"file": _upload(), "list": lists["newsletter"].public_id, "action": "preview", "agreed": "1"}
    token = editor_client.post("/people/import/", data).context["token"]
    response = editor_client.post("/people/import/", {"token": token, "list": lists["newsletter"].public_id, "action": "commit"})
    assert response.status_code == 400
    assert Person.objects.count() == 0


def test_import_previews_then_adds_people_with_import_consent(editor_client, editor, lists):
    existing = _person("james.whitfield@example.com", "", "", "en")
    data = {"file": _upload(), "list": lists["newsletter"].public_id, "action": "preview", "agreed": "1"}
    response = editor_client.post("/people/import/", data)
    assert response.status_code == 200
    plan = response.context["plan"]
    assert (plan.new, plan.updated, plan.skipped) == (1, 1, 2)
    assert len(response.context["preview"]) == 4
    assert Person.objects.count() == 1  # nothing saved by the preview

    response = editor_client.post("/people/import/", {
        "token": response.context["token"], "list": lists["newsletter"].public_id, "action": "commit", "agreed": "1"})
    assert response.status_code == 302
    olena = Person.objects.get(email="olena.k@example.com")
    assert (olena.first_name, olena.language.code, olena.source) == ("Olena", "uk", "import")
    assert olena.attributes == {"Neighbourhood": "Maple Ridge"}
    existing.refresh_from_db()
    assert existing.first_name == "James" and existing.attributes == {"Neighbourhood": "Alder Park"}
    for person in (olena, existing):
        sub = Subscription.objects.get(person=person, list=lists["newsletter"], channel="email")
        assert sub.state == "on"
        assert sub.consent.method == "import" and sub.consent.recorded_by == editor
        assert "agreed" in sub.consent.wording
    assert Person.objects.count() == 2


def test_import_skips_deleted_and_unsubscribed_people_and_says_so(editor_client, lists):
    services.delete_person(_person("gone@example.com"))
    unsubscribed = _person("camille.t@example.com", "Camille", "Tremblay", "fr")
    _subscribe(unsubscribed, lists["newsletter"])
    services.set_subscription(unsubscribed, lists["newsletter"], "email", False)
    text = "email\ngone@example.com\ncamille.t@example.com\npriya.nair@example.com\n"
    data = {"file": _upload(text), "list": lists["newsletter"].public_id, "action": "preview", "agreed": "1"}
    response = editor_client.post("/people/import/", data)
    plan = response.context["plan"]
    assert (plan.new, plan.updated, plan.skipped, plan.suppressed) == (1, 0, 2, 1)
    assert b"deleted" in response.content

    editor_client.post("/people/import/", {"token": response.context["token"], "list": lists["newsletter"].public_id,
                                           "action": "commit", "agreed": "1"})
    assert not Person.objects.filter(email="gone@example.com").exists()
    assert Subscription.objects.get(person=unsubscribed, list=lists["newsletter"], channel="email").state == "off"
    assert Person.objects.filter(email="priya.nair@example.com").exists()


def test_import_needs_an_email_column_and_a_campaign_list(editor_client, lists):
    data = {"file": _upload("name,phone\nOlena,555\n"), "list": lists["newsletter"].public_id, "action": "preview", "agreed": "1"}
    response = editor_client.post("/people/import/", data)
    assert response.status_code == 400 and b"email" in response.content
    data = {"file": _upload(), "list": lists["volunteers"].public_id, "action": "preview", "agreed": "1"}
    assert editor_client.post("/people/import/", data).status_code == 400
    assert Person.objects.count() == 0


# --------------------------------------------------------------------------- lists

def test_an_app_list_cannot_be_edited_by_staff(editor_client, lists):
    app_list = lists["volunteers"]
    person = _person()
    services.set_subscription(person, app_list, "email", True)

    assert editor_client.get(f"/people/lists/{app_list.public_id}/edit/").status_code == 403
    response = editor_client.post(f"/people/lists/{app_list.public_id}/edit/", {"name_en": "Renamed", "slug": "renamed"})
    assert response.status_code == 403
    assert editor_client.post(f"/people/lists/{app_list.public_id}/delete/").status_code == 403
    app_list.refresh_from_db()
    assert app_list.name == {"en": "Volunteers"} and app_list.slug == "volunteers"

    # Nor its members: staff can neither remove nor add someone.
    for on in ("0", "1"):
        response = editor_client.post(f"/people/{person.public_id}/subscription/",
                                      {"list": app_list.public_id, "channel": "email", "on": on, "note": "asked"})
        assert response.status_code == 403
    assert Subscription.objects.get(person=person, list=app_list, channel="email").state == "on"

    # The People screen shows it locked, with no edit button.
    page = editor_client.get("/people/").content.decode()
    assert "Volunteers" in page and f"/people/lists/{app_list.public_id}/edit/" not in page
    assert f"/people/lists/{lists['newsletter'].public_id}/edit/" in page


def test_staff_create_rename_and_delete_a_campaign_list(editor_client, lists):
    response = editor_client.post("/people/lists/new/", {
        "name_en": "Volunteer opportunities", "name_uk": "Можливості для волонтерів", "description_en": "When we need a hand",
        "slug": "", "on_signup_form": "1"})
    assert response.status_code == 302
    lst = List.objects.get(slug="volunteer-opportunities")
    assert lst.kind == "campaign" and lst.on_signup_form
    assert lst.name == {"en": "Volunteer opportunities", "uk": "Можливості для волонтерів"}
    assert lst.description == {"en": "When we need a hand"}

    response = editor_client.post(f"/people/lists/{lst.public_id}/edit/", {"name_en": "Helping out", "slug": "volunteer-opportunities"})
    assert response.status_code == 302
    lst.refresh_from_db()
    assert lst.name == {"en": "Helping out"} and not lst.on_signup_form

    # A name in the default language is required, and a slug cannot be reused.
    response = editor_client.post(f"/people/lists/{lst.public_id}/edit/", {"name_uk": "Тільки українською", "slug": "newsletter"})
    assert response.status_code == 200 and b"Give the list a name" in response.content
    response = editor_client.post(f"/people/lists/{lst.public_id}/edit/", {"name_en": "Helping out", "slug": "newsletter"})
    assert b"already uses" in response.content

    assert editor_client.post(f"/people/lists/{lst.public_id}/delete/").status_code == 302
    assert not List.objects.filter(pk=lst.pk).exists()


# --------------------------------------------------------------------------- Person

def test_a_persons_grid_reflects_their_subscriptions(editor_client, lists):
    person = _person()
    _subscribe(person, lists["newsletter"], "email")
    _subscribe(person, lists["events"], "email")
    services.set_subscription(person, lists["events"], "email", False)
    services.set_subscription(person, lists["volunteers"], "email", True)

    response = editor_client.get(f"/people/{person.public_id}/")
    assert response.status_code == 200
    grid = response.context["grid"]
    rows = {row["list"].slug: {cell["channel"]: cell["state"] for cell in row["cells"]} for row in grid["campaign"] + grid["app"]}
    assert rows["newsletter"]["email"] == "on"
    assert rows["events"]["email"] == "off"
    assert rows["pavilion-news"]["email"] is None
    assert rows["volunteers"]["email"] == "on"
    assert [row["list"].slug for row in grid["app"]] == ["volunteers"]

    page = response.content.decode()
    assert 'aria-pressed="true" aria-label="Monthly newsletter by Email"' in page
    assert 'aria-pressed="false" aria-label="Event updates by Email"' in page
    assert 'aria-label="Volunteers by Email: on, read-only"' in page
    assert "Ticked the consent box on the signup form" in page

    # Someone no building added does not show app lists at all.
    other = _person("priya.nair@example.com", "Priya", "Nair", "en")
    assert editor_client.get(f"/people/{other.public_id}/").context["grid"]["app"] == []


def test_staff_cannot_switch_a_newsletter_on_without_a_note(editor_client, editor, lists):
    person = _person()
    url = f"/people/{person.public_id}/subscription/"
    data = {"list": lists["newsletter"].public_id, "channel": "email", "on": "1"}

    assert editor_client.post(url, data).status_code == 400
    response = editor_client.post(url, {**data, "note": "   "}, HTTP_HX_REQUEST="true")
    assert b"Say how this person gave consent" in response.content
    assert not Subscription.objects.filter(person=person).exists()
    assert not ConsentRecord.objects.filter(person=person).exists()

    response = editor_client.post(url, {**data, "note": "Asked at the front desk on Nov 20."}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and b'hx-swap-oob="outerHTML"' in response.content
    sub = Subscription.objects.get(person=person, list=lists["newsletter"], channel="email")
    assert sub.state == "on"
    assert (sub.consent.method, sub.consent.note, sub.consent.recorded_by) == ("staff", "Asked at the front desk on Nov 20.", editor)

    # Switching off needs nothing.
    response = editor_client.post(url, {**data, "on": "0"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    sub.refresh_from_db()
    assert sub.state == "off"


def test_the_consent_dialog_asks_for_the_note(editor_client, lists):
    person = _person()
    response = editor_client.get(f"/people/{person.public_id}/subscription/?list={lists['newsletter'].public_id}&channel=email")
    assert response.status_code == 200 and b'name="note"' in response.content and b"required" in response.content


def test_editing_a_profile_and_its_extra_details(editor_client, lists):
    person = _person()
    person.email_state = "bounced"
    person.save()
    response = editor_client.post(f"/people/{person.public_id}/", {
        "first_name": "Olena", "last_name": "Kovalenko", "email": "Olena.New@Example.com", "language": "fr",
        "attr_key": ["Neighbourhood", "", "Volunteer since"], "attr_value": ["Maple Ridge", "ignored", "2024"]})
    assert response.status_code == 302
    person.refresh_from_db()
    assert person.email == "olena.new@example.com" and person.language.code == "fr"
    assert person.email_state == "ok"  # a new address starts fresh
    assert person.attributes == {"Neighbourhood": "Maple Ridge", "Volunteer since": "2024"}

    _person("taken@example.com", "Taras", "Melnyk")
    response = editor_client.post(f"/people/{person.public_id}/", {"first_name": "Olena", "email": "taken@example.com", "language": "fr"})
    assert response.status_code == 400 and b"already has this email" in response.content


def test_clearing_a_bounce_and_getting_the_telegram_connect_link(editor_client, lists):
    from channels.models import TelegramBot
    from people.tokens import person_from_connect_token

    person = _person()
    person.email_state = "bounced"
    person.save()
    page = editor_client.get(f"/people/{person.public_id}/").content.decode()
    assert "Bounced" in page and "Mark email as working" in page and "Get the connect link" not in page

    assert editor_client.post(f"/people/{person.public_id}/email-working/").status_code == 302
    person.refresh_from_db()
    assert person.email_state == "ok"

    TelegramBot.objects.create(name="Maple Grove", username="maplegrove_bot", token="123:abc", is_default=True)
    assert "Get the connect link" in editor_client.get(f"/people/{person.public_id}/").content.decode()
    response = editor_client.post(f"/people/{person.public_id}/connect-link/")
    link = response.content.decode().split('value="')[1].split('"')[0]
    assert link.startswith("https://t.me/maplegrove_bot?start=")
    assert person_from_connect_token(link.split("start=")[1]) == person


def test_stopping_all_newsletters_and_deleting_a_person(editor_client, lists):
    person = _person()
    _subscribe(person, lists["newsletter"], "email")
    _subscribe(person, lists["newsletter"], "telegram")
    services.set_subscription(person, lists["volunteers"], "email", True)

    assert editor_client.post(f"/people/{person.public_id}/stop-news/").status_code == 302
    states = {(s.list.slug, s.channel): s.state for s in person.subscriptions.select_related("list")}
    assert states == {("newsletter", "email"): "off", ("newsletter", "telegram"): "off", ("volunteers", "email"): "on"}

    assert editor_client.post(f"/people/{person.public_id}/delete/").status_code == 302
    assert not Person.objects.filter(pk=person.pk).exists()
    assert Suppression.is_suppressed("Olena.K@example.com")


def test_adding_a_person_by_hand(editor_client, lists):
    response = editor_client.post("/people/new/", {"first_name": "Daniel", "last_name": "Okafor", "email": "Daniel.Okafor@example.com", "language": "en"})
    person = Person.objects.get(email="daniel.okafor@example.com")
    assert response.status_code == 302 and response["Location"] == f"/people/{person.public_id}/"
    assert person.source == "staff" and not person.subscriptions.exists()

    response = editor_client.post("/people/new/", {"first_name": "Dan", "email": "daniel.okafor@example.com", "language": "en"})
    assert response.status_code == 200 and b"already here" in response.content
    assert Person.objects.count() == 1


def test_the_person_history_lists_messages_and_links_failures_to_the_outbox(editor_client, lists):
    from delivery.models import Message

    person = _person()
    Message.objects.create(person=person, source="system", channel="email", state="sent", subject_snapshot="Winter coat drive",
                           idempotency_key="t:1", sent_at=timezone.now())
    Message.objects.create(person=person, source="system", channel="email", state="failed", subject_snapshot="Shift reminder",
                           idempotency_key="t:2", error="The mailbox is full.")
    page = editor_client.get(f"/people/{person.public_id}/").content.decode()
    assert "Winter coat drive" in page and "Delivered" in page
    assert "Shift reminder" in page and "The mailbox is full." in page
    assert "/outbox/?status=failed" in page


# --------------------------------------------------------------------------- roles

def test_a_viewer_cannot_edit_people(viewer_client, lists):
    person = _person()
    _subscribe(person, lists["newsletter"])
    assert viewer_client.get("/people/").status_code == 200
    page = viewer_client.get(f"/people/{person.public_id}/")
    assert page.status_code == 200
    text = page.content.decode()
    assert "Delete this person" not in text and "Stop all newsletters" not in text and 'class="sg__tick"' not in text

    sub = {"list": lists["newsletter"].public_id, "channel": "email", "on": "0"}
    forbidden = [
        ("post", f"/people/{person.public_id}/", {"first_name": "Changed", "language": "en"}),
        ("post", f"/people/{person.public_id}/subscription/", sub),
        ("post", f"/people/{person.public_id}/stop-news/", {}),
        ("post", f"/people/{person.public_id}/delete/", {}),
        ("post", f"/people/{person.public_id}/email-working/", {}),
        ("post", f"/people/{person.public_id}/connect-link/", {}),
        ("post", "/people/new/", {"first_name": "New", "email": "new@example.com"}),
        ("get", "/people/import/", {}),
        ("post", "/people/import/", {"agreed": "1"}),
        ("post", "/people/lists/new/", {"name_en": "Sneaky"}),
        ("post", f"/people/lists/{lists['newsletter'].public_id}/edit/", {"name_en": "Sneaky"}),
        ("post", f"/people/lists/{lists['newsletter'].public_id}/delete/", {}),
        ("get", "/people/signup-form/", {}),
        ("post", "/people/signup-form/", {}),
    ]
    for method, url, data in forbidden:
        assert getattr(viewer_client, method)(url, data).status_code == 403, url
    person.refresh_from_db()
    assert person.first_name == "Olena" and Person.objects.count() == 1
    assert Subscription.objects.get(person=person).state == "on"
    assert List.objects.count() == 4


def test_people_pages_need_a_sign_in(client, org):
    response = client.get("/people/")
    assert response.status_code == 302 and response["Location"].startswith("/sign-in/")


# --------------------------------------------------------------------------- the People screen

def _many(n):
    Person.objects.bulk_create([
        Person(email=f"person{i:03d}@example.com", first_name="Person", last_name=f"Number{i:03d}", language=None) for i in range(n)])
    # bulk_create skips save(), which is what gives people the default language.
    from core.models import Language

    Person.objects.update(language=Language.default())


def test_pagination_shows_25_people_per_page_with_numbered_pages(viewer_client, lists):
    _many(60)
    first = viewer_client.get("/people/")
    assert len(first.context["people"]) == 25
    assert first.context["page"].paginator.num_pages == 3
    html = first.content.decode()
    assert "1 to 25 of 60" in html
    assert 'aria-current="page">1<' in html and 'href="?page=2"' in html and 'href="?page=3"' in html

    last = viewer_client.get("/people/?page=3")
    assert len(last.context["people"]) == 10
    assert "51 to 60 of 60" in last.content.decode()
    seen = {p.pk for page in (1, 2, 3) for p in viewer_client.get(f"/people/?page={page}").context["people"]}
    assert len(seen) == 60

    # Page links keep the search.
    found = viewer_client.get("/people/?q=person0")
    assert found.context["page"].paginator.count == 60
    assert "q=person0&amp;page=2" in found.content.decode() or "page=2&amp;q=person0" in found.content.decode()


def test_search_filters_and_lists_narrow_the_table(viewer_client, lists):
    from channels.models import TelegramBot, TelegramLink

    olena = _person()
    james = _person("james.whitfield@example.com", "James", "Whitfield", "en")
    iryna = _person("iryna.sh@example.com", "Iryna", "Shevchuk", "uk")
    iryna.email_state = "bounced"
    iryna.save()
    _subscribe(olena, lists["newsletter"])
    services.set_subscription(james, lists["volunteers"], "email", True)
    bot = TelegramBot.objects.create(name="Maple Grove", username="maplegrove_bot", token="123:abc")
    TelegramLink.objects.create(person=olena, bot=bot, chat_id=1)

    def emails(query):
        return sorted(p.email for p in viewer_client.get(f"/people/{query}").context["people"])

    assert emails("?q=whitfield") == ["james.whitfield@example.com"]
    assert emails("?q=OLENA.K@") == ["olena.k@example.com"]
    assert emails("?q=Olena Kovalenko") == ["olena.k@example.com"]
    assert emails("?lang=uk") == ["iryna.sh@example.com", "olena.k@example.com"]
    assert emails("?channel=telegram") == ["olena.k@example.com"]
    assert emails("?channel=email") == ["james.whitfield@example.com", "olena.k@example.com"]
    assert emails("?status=subscribed") == ["olena.k@example.com"]
    assert emails("?status=service") == ["james.whitfield@example.com"]
    assert emails("?status=bounced") == ["iryna.sh@example.com"]
    assert emails(f"?list={lists['newsletter'].public_id}") == ["olena.k@example.com"]
    assert emails(f"?list={lists['volunteers'].public_id}") == ["james.whitfield@example.com"]

    page = viewer_client.get("/people/")
    row = next(p for p in page.context["people"] if p.pk == olena.pk)
    assert [r["channel"] for r in row.reach] == ["email", "telegram"]
    assert row.list_names == "Monthly newsletter" and row.status[0] == "subscribed"
    counts = {l.slug: l.count for l in page.context["campaign_lists"] + page.context["app_lists"]}
    assert counts == {"newsletter": 1, "events": 0, "pavilion-news": 0, "volunteers": 1}


# --------------------------------------------------------------------------- signup form settings

def test_signup_form_settings_save_lists_and_switches(editor_client, lists):
    page = editor_client.get("/people/signup-form/")
    assert page.status_code == 200 and b"Pre-filled link" in page.content
    response = editor_client.post("/people/signup-form/", {"lists": ["newsletter", "pavilion-news"], "show_language": "1"})
    assert response.status_code == 302
    on_form = set(List.objects.filter(on_signup_form=True).values_list("slug", flat=True))
    assert on_form == {"newsletter", "pavilion-news"}
    org = Organization.get()
    assert org.signup_form == {"show_language": True, "show_telegram": False, "show_first_name": False}
    # An app list can never be put on the form, whatever is posted.
    editor_client.post("/people/signup-form/", {"lists": ["volunteers"]})
    assert not List.objects.get(slug="volunteers").on_signup_form


# --------------------------------------------------------------------------- the daily clean-up

def test_unconfirmed_subscriptions_are_deleted_after_14_days(lists):
    person = _person()
    consent = services.record_consent(person, [lists["newsletter"], lists["events"]], "signup_form", "I agree.")
    old = services.set_subscription(person, lists["newsletter"], "email", True, consent=consent, unconfirmed=True)
    fresh = services.set_subscription(person, lists["events"], "email", True, consent=consent, unconfirmed=True)
    confirmed = _subscribe(person, lists["pavilion"])
    long_ago = timezone.now() - timedelta(days=15)
    Subscription.objects.filter(pk__in=[old.pk, confirmed.pk]).update(updated_at=long_ago)

    assert delete_unconfirmed_subscriptions() == 1
    assert not Subscription.objects.filter(pk=old.pk).exists()
    assert Subscription.objects.filter(pk__in=[fresh.pk, confirmed.pk]).count() == 2


# --------------------------------------------------------------------------- every screen renders

def test_every_people_screen_and_dialog_renders(editor_client, viewer_client, lists):
    from channels.models import PushApp, PushMember, TelegramBot, TelegramLink

    person = _person()
    _subscribe(person, lists["newsletter"])
    services.set_subscription(person, lists["volunteers"], "telegram", True)
    bot = TelegramBot.objects.create(name="Maple Grove", username="maplegrove_bot", token="123:abc")
    TelegramLink.objects.create(person=person, bot=bot, chat_id=7)
    app = PushApp.objects.create(building=lists["volunteers"].building, audience="news", label="Offers and news",
                                 endpoint="https://townhall.example/push", list=lists["pavilion"])
    PushMember.objects.create(person=person, push_app=app, external_id="vol_88")
    urls = [
        "/people/", f"/people/?list={lists['volunteers'].public_id}", f"/people/?list={lists['newsletter'].public_id}&q=zzz",
        "/people/import/", "/people/new/", "/people/lists/new/", f"/people/lists/{lists['newsletter'].public_id}/edit/",
        f"/people/{person.public_id}/", f"/people/{person.public_id}/?history=all", "/people/signup-form/",
    ]
    for url in urls:
        assert editor_client.get(url).status_code == 200, url
    page = editor_client.get(f"/people/{person.public_id}/").content.decode()
    assert "Town Hall, Offers and news" in page and "Connected" in page
    assert 'aria-label="Pavilion news by Push"' in page  # push is offered where a push app serves the list
    assert viewer_client.get(f"/people/?list={lists['volunteers'].public_id}").status_code == 200
    assert editor_client.get("/people/p_999999/").status_code == 404
