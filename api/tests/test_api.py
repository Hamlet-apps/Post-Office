from unittest import mock

import pytest
from rest_framework.test import APIClient

from channels.models import PushMember
from commands.models import CommandLog
from content.models import Template
from core.models import ApiKey, Language
from delivery.models import Message, SendRequest
from delivery.tests import helpers as h
from people.models import ConsentRecord, List, Person, PersonExternalId, Subscription
from people.tokens import person_from_preferences_token

pytestmark = pytest.mark.django_db

ORDER = {
    "template": "pavilion.order_confirmation",
    "to": {"external_id": "cust_511", "email": "olena.k@example.com", "first_name": "Olena", "language": "uk"},
    "data": {"order": {"number": "#0142", "ready_time": "6:40 PM"}},
    "idempotency_key": "order-0142-confirmation",
}


def _client(key=None):
    client = APIClient()
    if key:
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
    return client


@pytest.fixture
def pavilion(org):
    building, key = h.make_building()
    h.make_template(building, notification=False)
    h.make_sender("app")
    return building, _client(key)


def test_health_needs_no_key(org):
    response = _client().get("/api/v1/health")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_calls_without_a_valid_key_are_refused_in_the_error_shape(org):
    response = _client().post("/api/v1/messages", ORDER, format="json")
    assert response.status_code == 401 and response.json()["error"]["code"] == "not_authenticated"
    response = _client("po_bld_wrong").post("/api/v1/messages", ORDER, format="json")
    assert response.status_code == 401 and set(response.json()["error"]) == {"code", "message"}


def test_the_same_idempotency_key_sent_twice_produces_one_email(pavilion, smtp):
    building, client = pavilion
    first = client.post("/api/v1/messages", ORDER, format="json")
    assert first.status_code == 202
    answer = first.json()
    assert answer["id"].startswith("msg_") and answer["channels"] == ["email"]
    second = client.post("/api/v1/messages", ORDER, format="json")
    assert second.status_code == 202 and second.json() == answer
    assert len(smtp.sent) == 1 and SendRequest.objects.count() == 1 and Message.objects.count() == 1
    assert smtp.sent[0][1]["Subject"] == "Order #0142 (Olena)"

    # The person was created, with the language the building gave, and joined the app list.
    person = Person.objects.get()
    assert person.email == "olena.k@example.com" and person.language.code == "uk" and person.source == "Pavilion"
    assert PersonExternalId.objects.get(building=building).external_id == "cust_511"
    assert Subscription.objects.filter(person=person, list__kind="app", channel="email", state="on").exists()

    state = client.get(f"/api/v1/messages/{answer['id']}")
    assert state.status_code == 200
    assert state.json()["channels"] == [{"channel": "email", "state": "sent", "language": "en", "used_fallback": True,
                                         "error": "", "sent_at": mock.ANY}]


def test_to_is_matched_by_external_id_first_and_language_is_kept(pavilion, smtp):
    building, client = pavilion
    client.post("/api/v1/messages", ORDER, format="json")
    again = dict(ORDER, idempotency_key="order-0143", to={"external_id": "cust_511", "email": "other@example.com", "language": "fr"})
    assert client.post("/api/v1/messages", again, format="json").status_code == 202
    person = Person.objects.get()
    assert person.email == "olena.k@example.com" and person.language.code == "uk"
    by_email = dict(ORDER, idempotency_key="order-0144", to={"email": "Olena.K@example.com"})
    client.post("/api/v1/messages", by_email, format="json")
    assert Person.objects.count() == 1 and len(smtp.sent) == 3


def test_messages_needs_a_template_a_recipient_and_a_key(pavilion, smtp):
    _building, client = pavilion
    unknown = client.post("/api/v1/messages", dict(ORDER, template="pavilion.nope"), format="json")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_template"
    no_key = client.post("/api/v1/messages", {k: v for k, v in ORDER.items() if k != "idempotency_key"}, format="json")
    assert no_key.status_code == 400 and no_key.json()["error"]["code"] == "idempotency_key_required"
    nobody = client.post("/api/v1/messages", dict(ORDER, to={"first_name": "Olena"}), format="json")
    assert nobody.status_code == 400 and nobody.json()["error"]["code"] == "invalid"
    other, other_key = h.make_building("Town Hall", "town_hall")
    theirs = _client(other_key).post("/api/v1/messages", ORDER, format="json")
    assert theirs.status_code == 403
    assert smtp.sent == [] and Person.objects.count() == 0


def test_an_api_key_with_scope_subscribe_cannot_send(pavilion, smtp):
    _key_row, subscribe_key = ApiKey.issue("Website form", "subscribe")
    response = _client(subscribe_key).post("/api/v1/messages", ORDER, format="json")
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert smtp.sent == [] and SendRequest.objects.count() == 0
    _key_row, send_key = ApiKey.issue("Cron job", "send")
    assert _client(send_key).post("/api/v1/messages", ORDER, format="json").status_code == 202
    assert len(smtp.sent) == 1
    # ...and a send key cannot subscribe people.
    refused = _client(send_key).post("/api/v1/subscribe", {"email": "a@example.com", "consent": True, "wording": "Yes"}, format="json")
    assert refused.status_code == 403


def test_put_templates_registers_the_buildings_definitions(pavilion):
    building, client = pavilion
    definitions = [{"key": "pavilion.order_ready", "name": "Order ready", "fires": "When an order is ready",
                    "variables": {"order.number": {"label": "order number", "sample": "#0142"}},
                    "content": {"en": {"email": {"subject": "Ready", "blocks": [{"type": "text", "text": "Come by."}]}}}}]
    response = client.put("/api/v1/templates", {"templates": definitions}, format="json")
    assert response.status_code == 200 and response.json()["created"] == 1
    assert Template.objects.get(key="pavilion.order_ready").building == building
    with mock.patch("content.services.register_templates", return_value={"created": 0, "updated": 1, "kept": 0}) as register:
        assert client.put("/api/v1/templates", definitions, format="json").json()["updated"] == 1
    assert register.call_args[0] == (building, definitions)
    _row, key = ApiKey.issue("Other", "all")
    assert _client(key).put("/api/v1/templates", definitions, format="json").status_code == 403


def test_lists_and_members_are_kept_in_step(pavilion):
    building, client = pavilion
    created = client.put("/api/v1/lists/volunteers", {"name": "Volunteers", "kind": "app"}, format="json")
    assert created.status_code == 201
    lst = List.objects.get(slug="volunteers")
    assert lst.kind == "app" and lst.building == building and lst.name == {"en": "Volunteers"} and not lst.on_signup_form
    assert client.put("/api/v1/lists/volunteers", {"name": {"en": "Volunteers", "uk": "Волонтери"}}, format="json").status_code == 200

    member = {"email": "taras.melnyk@example.com", "first_name": "Taras", "language": "uk", "attributes": {"volunteer_since": "2024"}}
    response = client.put("/api/v1/lists/volunteers/members/vol_88", member, format="json")
    assert response.status_code == 200 and response.json()["channels"]["email"] == "on"
    person = Person.objects.get(email="taras.melnyk@example.com")
    assert person.attributes == {"volunteer_since": "2024"} and person.language.code == "uk"
    assert lst.people().count() == 1
    client.put("/api/v1/lists/volunteers/members/vol_88", dict(member, first_name="Taras M."), format="json")
    assert Person.objects.count() == 1 and Person.objects.get().first_name == "Taras M."

    assert client.delete("/api/v1/lists/volunteers/members/vol_88").status_code == 204
    assert lst.people().count() == 0 and Person.objects.count() == 1

    _other, other_key = h.make_building("Town Hall", "town_hall")
    assert _client(other_key).put("/api/v1/lists/volunteers/members/x1", member, format="json").status_code == 403
    assert client.put("/api/v1/lists/unknown/members/x1", member, format="json").status_code == 404


def test_a_building_owned_campaign_list_needs_consent(pavilion):
    _building, client = pavilion
    client.put("/api/v1/lists/pavilion-news", {"name": "Pavilion news", "kind": "campaign"}, format="json")
    member = {"email": "olena.k@example.com", "first_name": "Olena"}
    refused = client.put("/api/v1/lists/pavilion-news/members/cust_511", member, format="json")
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "consent_required"
    assert Person.objects.count() == 0 and Subscription.objects.count() == 0
    consent = {"wording": "Send me Pavilion offers by email.", "given_at": "2026-11-02T18:40:00Z", "ip": "203.0.113.9"}
    accepted = client.put("/api/v1/lists/pavilion-news/members/cust_511", dict(member, channels=["email"], consent=consent), format="json")
    assert accepted.status_code == 200 and accepted.json()["channels"] == {"email": "on"}
    record = ConsentRecord.objects.get()
    assert record.method == "api" and record.wording == consent["wording"] and record.ip == "203.0.113.9"
    assert record.given_at.isoformat().startswith("2026-11-02T18:40")
    # Sending the same member again does not pile up consent records.
    client.put("/api/v1/lists/pavilion-news/members/cust_511", dict(member, channels=["email"], consent=consent), format="json")
    assert ConsentRecord.objects.count() == 1
    client.delete("/api/v1/lists/pavilion-news/members/cust_511")
    assert Subscription.objects.get().state == "off"


def test_push_members_follow_what_the_building_reports(pavilion):
    building, client = pavilion
    news = h.make_list("pavilion-offers", building=building, name="Offers and news")
    app = h.make_push_app(building, news)
    response = client.put("/api/v1/push-apps/offers-and-news/members/cust_511", {"email": "olena.k@example.com"}, format="json")
    assert response.status_code == 200 and response.json()["push"] == "on"
    member = PushMember.objects.get()
    assert member.push_app == app and member.external_id == "cust_511"
    assert Subscription.objects.get(person=member.person, list=news, channel="push").state == "on"
    client.put("/api/v1/push-apps/offers-and-news/members/cust_511", {}, format="json")
    assert PushMember.objects.count() == 1 and ConsentRecord.objects.count() == 1
    assert client.delete("/api/v1/push-apps/offers-and-news/members/cust_511").status_code == 204
    assert PushMember.objects.count() == 0
    assert Subscription.objects.get(list=news, channel="push").state == "off"
    assert client.put("/api/v1/push-apps/nope/members/cust_511", {}, format="json").status_code == 404


def test_subscribe_needs_true_consent_and_the_wording(org):
    _row, key = ApiKey.issue("Website form", "subscribe")
    client = _client(key)
    body = {"email": "new@example.com", "first_name": "Priya", "language": "en", "lists": ["newsletter"], "telegram": True}
    for bad in (dict(body), dict(body, consent="yes", wording="I agree"), dict(body, consent=True)):
        response = client.post("/api/v1/subscribe", bad, format="json")
        assert response.status_code == 422 and response.json()["error"]["code"] in ("consent_required", "wording_required")
    with mock.patch("people.signup.process_signup") as signup:
        response = client.post("/api/v1/subscribe", dict(body, consent=True, wording="Yes, send me the newsletter."),
                               format="json", HTTP_USER_AGENT="MapleSite/1")
    assert response.status_code == 200 and response.json() == {"ok": True}
    kwargs = signup.call_args.kwargs
    assert kwargs["email"] == "new@example.com" and kwargs["list_slugs"] == ["newsletter"] and kwargs["telegram"] is True
    assert kwargs["wording"] == "Yes, send me the newsletter." and kwargs["method"] == "api"
    assert kwargs["user_agent"] == "MapleSite/1" and kwargs["language_code"] == "en"


def test_preferences_link_for_a_person_the_building_knows(pavilion, smtp):
    _building, client = pavilion
    client.post("/api/v1/messages", ORDER, format="json")
    person = Person.objects.get()
    for person_id in ("cust_511", person.public_id):
        response = client.get(f"/api/v1/people/{person_id}/preferences-link")
        assert response.status_code == 200
        url = response.json()["url"]
        assert url.startswith("https://post.maplegrove.example/p/")
        assert person_from_preferences_token(url.rsplit("/", 1)[1]) == person
    stranger = h.make_person("stranger@example.com", "Stranger")
    assert client.get(f"/api/v1/people/{stranger.public_id}/preferences-link").status_code == 404
    assert client.get("/api/v1/people/cust_999/preferences-link").status_code == 404


def test_late_command_reply_by_token_or_key(org):
    bot = h.make_bot()
    log = CommandLog.objects.create(bot=bot, chat_id=5, text="/hours", command="hours", target="http")
    client = _client()
    with mock.patch("commands.handler.late_reply", return_value=True) as late:
        assert client.post(f"/api/v1/commands/{log.public_id}/reply?token=wrong", {"text": "Open 9 to 5"}, format="json").status_code == 403
        assert client.post(f"/api/v1/commands/{log.public_id}/reply", {"text": "Open 9 to 5"}, format="json").status_code == 401
        assert not late.called
        ok = client.post(f"/api/v1/commands/{log.public_id}/reply?token={log.reply_token}", {"text": "Open 9 to 5"}, format="json")
        assert ok.status_code == 200 and late.call_args[0] == (log, "Open 9 to 5")
    with mock.patch("commands.handler.late_reply", return_value=False):
        _row, key = ApiKey.issue("Switchboard", "all")
        late_again = _client(key).post(f"/api/v1/commands/{log.public_id}/reply", {"text": "Again"}, format="json")
        assert late_again.status_code == 409 and late_again.json()["error"]["code"] == "too_late"


def test_sixty_requests_per_second_per_key(pavilion):
    _building, client = pavilion
    with mock.patch("api.views.time.time", return_value=1_800_000_000.0):
        codes = [client.get("/api/v1/messages/msg_0").status_code for _n in range(61)]
    assert codes[:60] == [404] * 60 and codes[60] == 429
    assert Language.objects.count() == 3
