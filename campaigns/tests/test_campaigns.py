"""Campaign list, audience and send, scheduling and the report.

Sample data is the fictional Maple Grove Community Centre. Nothing here touches
the network: httpx and the delivery pipeline are patched.
"""
from datetime import timedelta
from unittest import mock

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from campaigns import checks, reports, services
from campaigns.models import Campaign
from campaigns.tasks import start_scheduled_campaigns
from channels.models import EmailSender
from content.models import Content
from core.models import Language, Organization, TeamMember
from core.views import save_languages
from delivery.models import LinkClick, Message, TrackedLink
from people.models import List, Person, Subscription
from people.services import record_consent, set_subscription

pytestmark = pytest.mark.django_db

LINK = "https://maplegrove.example/coat-drive"


def _response(status):
    return mock.Mock(status_code=status)


@pytest.fixture(autouse=True)
def _no_network():
    """Every link answers 200 unless a test says otherwise."""
    cache.clear()
    with mock.patch("httpx.head", return_value=_response(200)) as head, mock.patch("httpx.get", return_value=_response(200)):
        yield head


@pytest.fixture
def org():
    save_languages(["en", "uk", "fr"], "en")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.mailing_address = "12 Alder Street"
    org.timezone = "America/Vancouver"
    org.setup_complete = True
    org.save()
    TeamMember.objects.create_user("owner@maplegrove.example", "long password", role="owner", name="Olena")
    return org


def signed_in(role):
    user = TeamMember.objects.filter(role=role).first() or TeamMember.objects.create_user(
        f"{role}@maplegrove.example", "long password", role=role, name=role.title())
    client = Client()
    client.force_login(user)
    return client


def add_person(lst, email, language="en", consent=True, channel="email"):
    person = Person.objects.create(email=email, first_name=email.split("@")[0].title(), language=Language.objects.get(code=language))
    if consent:
        proof = record_consent(person, [lst], "signup_form", "Yes, send me the newsletter.", confirmed=True)
        set_subscription(person, lst, channel, True, consent=proof)
    else:
        Subscription.objects.create(person=person, list=lst, channel=channel, state="on")
    return person


def write(campaign, code, form="email"):
    if form == "email":
        return Content.objects.create(campaign=campaign, language=code, form="email", subject="Winter coat drive", blocks=[
            {"id": "b1", "type": "heading", "text": "Winter coat drive"},
            {"id": "b2", "type": "text", "text": "Hi {first_name}, bring a coat to [the front desk](https://maplegrove.example/drop-off)."},
            {"id": "b3", "type": "button", "text": "Donate a coat", "href": LINK},
        ])
    return Content.objects.create(campaign=campaign, language=code, form="notification", text="Coat drive this week.")


@pytest.fixture
def campaign(org):
    """A draft that is ready to go in English and Ukrainian, but not in French."""
    sender = EmailSender.objects.create(purpose="newsletter", from_name="Maple Grove News", from_email="news@maplegrove.example",
                                        is_default=True, state="ok")
    newsletter = List.objects.create(name={"en": "Newsletter"}, slug="newsletter", kind="campaign")
    add_person(newsletter, "olena@example.com", "uk")
    add_person(newsletter, "marc@example.com", "fr")
    add_person(newsletter, "dana@example.com", "en")
    campaign = Campaign.objects.create(name="Winter coat drive", channels=["email"], sender=sender)
    campaign.lists.add(newsletter)
    write(campaign, "en")
    write(campaign, "uk")
    return campaign


def schedule_tomorrow(client, campaign):
    day = (Organization.get().local_now() + timedelta(days=1)).strftime("%Y-%m-%d")
    return client.post(f"/campaigns/{campaign.public_id}/schedule/", {"when": "later", "date": day, "time": "09:00", "spread": "1"})


# --------------------------------------------------------------------------- scheduling rules

def test_hold_blocks_a_campaign_with_an_unwritten_language(org, campaign):
    org.missing_translation = "hold"
    org.save()
    ok, reasons = checks.can_schedule(campaign)
    assert not ok and any("French" in str(r) for r in reasons)
    response = schedule_tomorrow(signed_in("editor"), campaign)
    assert response.status_code == 302 and response["Location"].endswith("/send/")
    campaign.refresh_from_db()
    assert campaign.state == "draft" and campaign.scheduled_at is None


def test_fallback_lets_the_same_campaign_be_scheduled(org, campaign):
    assert org.missing_translation == "fallback"
    french = next(c for c in checks.checklist(campaign) if c.key == "language_fr")
    assert not french.ok and not french.blocking  # a warning, not a stop
    assert checks.can_schedule(campaign) == (True, [])
    response = schedule_tomorrow(signed_in("editor"), campaign)
    assert response.status_code == 302
    campaign.refresh_from_db()
    assert campaign.state == "scheduled" and campaign.spread_minutes == 60
    local = campaign.scheduled_at.astimezone(org.tz)
    assert (local.hour, local.minute) == (9, 0)
    assert local.date() == (org.local_now() + timedelta(days=1)).date()


def test_the_default_language_must_always_be_written(org, campaign):
    Content.objects.filter(campaign=campaign, language="en").delete()
    ok, reasons = checks.can_schedule(campaign)
    assert not ok and any("default language" in str(r) for r in reasons)
    schedule_tomorrow(signed_in("editor"), campaign)
    campaign.refresh_from_db()
    assert campaign.state == "draft"


def test_no_list_or_no_channel_blocks(org, campaign):
    campaign.lists.clear()
    assert not checks.can_schedule(campaign)[0]
    campaign.lists.add(List.objects.get(slug="newsletter"))
    campaign.channels = []
    campaign.save()
    ok, reasons = checks.can_schedule(campaign)
    assert not ok and any("channel" in str(r) for r in reasons)


def test_a_sender_that_is_not_working_blocks(org, campaign):
    campaign.sender.state = "error"
    campaign.sender.last_error = "The password was refused."
    campaign.sender.save()
    sender = next(c for c in checks.checklist(campaign) if c.key == "sender")
    assert not sender.ok and sender.blocking and "refused" in sender.detail
    assert not checks.can_schedule(campaign)[0]


def test_a_viewer_cannot_schedule(org, campaign):
    viewer = signed_in("viewer")
    assert schedule_tomorrow(viewer, campaign).status_code == 403
    assert viewer.post(f"/campaigns/{campaign.public_id}/schedule/", {"when": "now"}).status_code == 403
    assert viewer.get(f"/campaigns/{campaign.public_id}/send/").status_code == 403
    campaign.refresh_from_db()
    assert campaign.state == "draft"
    assert viewer.get("/campaigns/").status_code == 200


def test_a_time_in_the_past_is_refused(org, campaign):
    response = signed_in("editor").post(f"/campaigns/{campaign.public_id}/schedule/", {"when": "later", "date": "2020-01-01", "time": "09:00"})
    assert response.status_code == 302
    campaign.refresh_from_db()
    assert campaign.state == "draft"


# --------------------------------------------------------------------------- starting the send

def test_due_campaigns_are_handed_to_the_pipeline(org, campaign):
    schedule_tomorrow(signed_in("editor"), campaign)
    with mock.patch("delivery.pipeline.send_campaign") as send_campaign:
        assert start_scheduled_campaigns() == 0  # tomorrow is not due yet
        send_campaign.assert_not_called()
        Campaign.objects.filter(pk=campaign.pk).update(scheduled_at=timezone.now() - timedelta(minutes=1))
        assert start_scheduled_campaigns() == 1
        send_campaign.assert_called_once()
        assert send_campaign.call_args[0][0].pk == campaign.pk
        assert start_scheduled_campaigns() == 0  # already sending: not started twice
        send_campaign.assert_called_once()
    campaign.refresh_from_db()
    assert campaign.state == "sending" and campaign.started_at is not None


def test_send_now_starts_the_pipeline_and_opens_the_report(org, campaign):
    with mock.patch("delivery.pipeline.send_campaign") as send_campaign:
        response = signed_in("editor").post(f"/campaigns/{campaign.public_id}/schedule/", {"when": "now"})
    send_campaign.assert_called_once()
    assert response.status_code == 302 and response["Location"] == f"/campaigns/{campaign.public_id}/"
    campaign.refresh_from_db()
    assert campaign.state == "sending" and campaign.spread_minutes == 0


def test_pause_resume_and_cancel(org, campaign):
    editor = signed_in("editor")
    schedule_tomorrow(editor, campaign)
    editor.post(f"/campaigns/{campaign.public_id}/cancel/")
    campaign.refresh_from_db()
    assert campaign.state == "draft" and campaign.scheduled_at is None  # still scheduled: back to a draft
    Campaign.objects.filter(pk=campaign.pk).update(state="sending", started_at=timezone.now())
    with mock.patch("delivery.pipeline.pause_campaign") as pause, mock.patch("delivery.pipeline.resume_campaign") as resume, \
            mock.patch("delivery.pipeline.cancel_campaign") as cancel:
        editor.post(f"/campaigns/{campaign.public_id}/pause/")
        campaign.refresh_from_db()
        assert campaign.state == "paused" and pause.called
        editor.post(f"/campaigns/{campaign.public_id}/resume/")
        campaign.refresh_from_db()
        assert campaign.state == "sending" and resume.called
        editor.post(f"/campaigns/{campaign.public_id}/cancel/")
        campaign.refresh_from_db()
        assert campaign.state == "cancelled" and cancel.called


# --------------------------------------------------------------------------- checklist

def test_the_checklist_flags_a_broken_link(org, campaign, _no_network):
    assert sorted(url for url, _label in checks.collect_links(campaign)) == [LINK, "https://maplegrove.example/drop-off"]
    assert dict(checks.collect_links(campaign))[LINK] == "Donate a coat"

    def head(url, **kwargs):
        return _response(404 if url == LINK else 200)

    cache.clear()
    with mock.patch("httpx.head", side_effect=head), mock.patch("httpx.get", return_value=_response(404)):
        links = next(c for c in checks.checklist(campaign) if c.key == "links")
        assert not links.ok and links.blocking
        assert LINK in links.detail and "drop-off" not in links.detail
        cache.clear()
        ok, reasons = checks.can_schedule(campaign)
        assert not ok and any("link" in str(r) for r in reasons)
        schedule_tomorrow(signed_in("editor"), campaign)
    campaign.refresh_from_db()
    assert campaign.state == "draft"


def test_a_link_that_cannot_be_reached_is_broken_and_a_redirect_is_fine(org, campaign):
    cache.clear()
    with mock.patch("httpx.head", side_effect=httpx.ConnectError("no route")):
        assert set(checks.check_links([LINK])) == {LINK}
    cache.clear()
    with mock.patch("httpx.head", return_value=_response(301)):
        assert checks.check_links([LINK]) == {}
    cache.clear()
    with mock.patch("httpx.head", return_value=_response(405)), mock.patch("httpx.get", return_value=_response(200)):
        assert checks.check_links([LINK]) == {}  # a site that refuses HEAD is asked again with GET


def test_the_checklist_flags_missing_consent(org, campaign):
    consent = next(c for c in checks.checklist(campaign) if c.key == "consent")
    assert consent.ok
    add_person(List.objects.get(slug="newsletter"), "nobody-asked@example.com", consent=False)
    consent = next(c for c in checks.checklist(campaign) if c.key == "consent")
    assert not consent.ok and consent.blocking and "1 person" in consent.label
    assert not checks.can_schedule(campaign)[0]


def test_the_unsubscribe_footer_is_checked(org, campaign):
    footer = next(c for c in checks.checklist(campaign) if c.key == "unsubscribe")
    assert footer.ok
    assert checks.footer_missing_in(campaign) == []


def test_notification_form_is_needed_for_telegram(org, campaign):
    campaign.channels = ["email", "telegram"]
    campaign.save()
    assert checks.needed_forms(campaign) == ["email", "notification"]
    keys = {c.key: c for c in checks.checklist(campaign)}
    assert not keys["default_language"].ok and not keys["bot"].ok
    write(campaign, "en", "notification")
    rows = {r["language"].code: r for r in checks.language_rows(campaign, {"en": 1, "uk": 1, "fr": 1})}
    assert rows["en"]["complete"] and rows["en"]["gets"].code == "en"
    assert rows["uk"]["email"] and not rows["uk"]["notification"] and rows["uk"]["fallback"]
    assert rows["fr"]["fallback"] and rows["fr"]["gets"].code == "en" and rows["fr"]["readers"] == 1


# --------------------------------------------------------------------------- screens

def test_list_shows_states_tags_and_search(org, campaign):
    Campaign.objects.create(name="Spring fair save the date")
    client = signed_in("viewer")
    html = client.get("/campaigns/").content.decode()
    assert "Winter coat drive" in html and "Spring fair save the date" in html
    assert "cp-lang--missing" in html and "To Newsletter" in html
    assert "New campaign" not in html  # a viewer cannot start one
    html = client.get("/campaigns/?q=coat").content.decode()
    assert "Winter coat drive" in html and "Spring fair" not in html
    Campaign.objects.filter(pk=campaign.pk).update(state="sent")
    html = client.get("/campaigns/?state=draft").content.decode()
    assert "Spring fair" in html and "Winter coat drive" not in html
    assert "Nothing matches" in client.get("/campaigns/?q=zzz").content.decode()


def test_an_empty_list_reads_well(org):
    html = signed_in("editor").get("/campaigns/").content.decode()
    assert "No campaigns yet" in html and "Write your first campaign" in html


def test_new_duplicate_rename_and_delete(org, campaign):
    editor = signed_in("editor")
    response = editor.post("/campaigns/new/")
    draft = Campaign.objects.order_by("-pk").first()
    assert response.status_code == 302 and response["Location"] == f"/campaigns/{draft.public_id}/design/"
    assert draft.name == "Untitled campaign" and draft.channels == ["email"] and draft.sender == campaign.sender
    assert draft.state == "draft" and draft.created_by.role == "editor"
    assert editor.get("/campaigns/new/").status_code == 302  # a plain link works too

    editor.post(f"/campaigns/{draft.public_id}/rename/", {"name": "Spring fair save the date"})
    draft.refresh_from_db()
    assert draft.name == "Spring fair save the date"

    response = editor.post(f"/campaigns/{campaign.public_id}/duplicate/")
    copy = Campaign.objects.order_by("-pk").first()
    assert response["Location"] == f"/campaigns/{copy.public_id}/design/"
    assert copy.name == "Winter coat drive (copy)" and copy.state == "draft"
    assert list(copy.lists.all()) == list(campaign.lists.all())
    assert sorted(copy.contents.values_list("language", flat=True)) == ["en", "uk"]
    assert copy.contents.get(language="en").blocks == campaign.contents.get(language="en").blocks

    Campaign.objects.filter(pk=campaign.pk).update(state="sent")
    editor.post(f"/campaigns/{campaign.public_id}/delete/")
    assert Campaign.objects.filter(pk=campaign.pk).exists()  # only drafts can be deleted
    editor.post(f"/campaigns/{copy.public_id}/delete/")
    assert not Campaign.objects.filter(pk=copy.pk).exists()
    assert signed_in("viewer").post("/campaigns/new/").status_code == 403


def test_new_from_a_campaign_template(org, campaign):
    from content.services import save_as_campaign_template

    template = save_as_campaign_template(campaign, "Monthly newsletter")
    signed_in("editor").post("/campaigns/new/", {"template": template.key})
    draft = Campaign.objects.order_by("-pk").first()
    assert draft.pk != campaign.pk and draft.contents.get(language="uk", form="email").subject == "Winter coat drive"


def test_send_screen_and_autosave(org, campaign):
    editor = signed_in("editor")
    html = editor.get(f"/campaigns/{campaign.public_id}/send/").content.decode()
    for text in ("Who gets it", "Before you send", "Languages", "When", "Send yourself a test", "English (fallback)", "3 people"):
        assert text in html, text
    events = List.objects.create(name={"en": "Event updates"}, slug="events", kind="campaign")
    add_person(events, "ivan@example.com", "uk")
    newsletter = List.objects.get(slug="newsletter")
    response = editor.post(
        f"/campaigns/{campaign.public_id}/audience/",
        {"lists": [newsletter.public_id, events.public_id], "channels": ["email"], "sender": campaign.sender.public_id},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()
    assert response.status_code == 200 and "<html" not in html
    assert 'id="cs-who"' in html and 'hx-swap-oob="true"' in html and 'id="cs-checks"' in html and "4 people" in html
    campaign.refresh_from_db()
    assert set(campaign.lists.all()) == {newsletter, events} and campaign.channels == ["email"]

    response = editor.post(f"/campaigns/{campaign.public_id}/audience/", {"lists": [], "channels": []}, HTTP_HX_REQUEST="true")
    campaign.refresh_from_db()
    assert campaign.channels == [] and campaign.sender is None and not campaign.lists.exists()
    assert "Choose at least one list" in response.content.decode()


def test_report_redirects_until_the_campaign_has_gone_out(org, campaign):
    viewer = signed_in("viewer")
    assert viewer.get(f"/campaigns/{campaign.public_id}/")["Location"].endswith("/design/")
    Campaign.objects.filter(pk=campaign.pk).update(state="scheduled", scheduled_at=timezone.now() + timedelta(days=1))
    assert viewer.get(f"/campaigns/{campaign.public_id}/")["Location"].endswith("/send/")
    assert signed_in("editor").get(f"/campaigns/{campaign.public_id}/send/").status_code == 200
    assert viewer.get("/campaigns/cmp_999999/").status_code == 404


def test_a_test_email_goes_out_in_every_written_language(org, campaign):
    editor = signed_in("editor")
    with mock.patch("delivery.pipeline.send_test_email", return_value=(True, "")) as send_test:
        response = editor.post(f"/campaigns/{campaign.public_id}/test/", {"email": "andrew@maplegrove.example", "language": "all"},
                               HTTP_HX_REQUEST="true")
        assert send_test.call_count == 2 and "2 test messages" in response.content.decode()
        sender, to_email, rendered = send_test.call_args_list[0][0]
        assert sender == campaign.sender and to_email == "andrew@maplegrove.example"
        assert rendered.subject == "[Test] Winter coat drive" and "Unsubscribe" in rendered.html
        send_test.reset_mock()
        editor.post(f"/campaigns/{campaign.public_id}/test/", {"email": "andrew@maplegrove.example", "language": "uk"}, HTTP_HX_REQUEST="true")
        assert send_test.call_count == 1
    with mock.patch("delivery.pipeline.send_test_email", return_value=(False, "The mail server refused the password.")):
        response = editor.post(f"/campaigns/{campaign.public_id}/test/", {"email": "andrew@maplegrove.example"}, HTTP_HX_REQUEST="true")
        assert "refused the password" in response.content.decode()


# --------------------------------------------------------------------------- report

def _message(campaign, person, channel="email", state="sent", **extra):
    return Message.objects.create(
        person=person, source="campaign", message_type="newsletter", campaign=campaign, channel=channel,
        language=extra.pop("language", person.language_code), state=state,
        idempotency_key=f"{campaign.public_id}:{person.public_id}:{channel}", **extra)


@pytest.fixture
def sent_campaign(org, campaign):
    """Three emails delivered (one opened, one opened and clicked), one failed, one Telegram tap, one skipped."""
    start = timezone.now() - timedelta(hours=30)
    Campaign.objects.filter(pk=campaign.pk).update(state="sent", started_at=start, sent_at=start, channels=["email", "telegram"])
    campaign.refresh_from_db()
    newsletter = List.objects.get(slug="newsletter")
    olena, marc, dana = (Person.objects.get(email=e) for e in ("olena@example.com", "marc@example.com", "dana@example.com"))
    bounce = add_person(newsletter, "gone@example.com")
    quiet = add_person(newsletter, "quiet@example.com")
    _message(campaign, dana, state="sent", sent_at=start)
    _message(campaign, olena, state="opened", sent_at=start, opened_at=start + timedelta(minutes=20))
    clicked = _message(campaign, marc, state="clicked", language="en", used_fallback=True, sent_at=start,
                       opened_at=start + timedelta(hours=2, minutes=5), clicked_at=start + timedelta(hours=2, minutes=6))
    _message(campaign, bounce, state="failed", error="Address does not exist")
    tapped = _message(campaign, olena, channel="telegram", state="clicked", sent_at=start, clicked_at=start + timedelta(hours=1))
    _message(campaign, quiet, channel="telegram", state="skipped", error="Has not connected Telegram")
    link = TrackedLink.objects.create(campaign=campaign, url=LINK)
    other = TrackedLink.objects.create(campaign=campaign, url="https://maplegrove.example/drop-off")
    LinkClick.objects.create(message=clicked, link=link)
    LinkClick.objects.create(message=tapped, link=link)
    LinkClick.objects.create(message=clicked, link=other)
    # Dana unsubscribes after getting it; someone who never got it does not count.
    Subscription.objects.filter(person=dana, list=newsletter).update(state="off", updated_at=timezone.now())
    Subscription.objects.filter(person=quiet, list=newsletter).update(state="off", updated_at=timezone.now())
    return campaign


def test_report_totals_match_the_message_rows(sent_campaign):
    report = reports.campaign_report(sent_campaign)
    stats = report["stats"]
    rows = Message.objects.filter(campaign=sent_campaign)
    assert stats["sent"] == rows.exclude(state="skipped").count() == 5
    assert stats["delivered"] == rows.filter(state__in=Message.DELIVERED_STATES).count() == 4
    assert stats["opened"] == rows.filter(opened_at__isnull=False).count() == 2
    assert stats["clicked"] == rows.filter(clicked_at__isnull=False).count() == 2
    assert stats["failed"] == 1 and stats["skipped"] == 1
    assert stats["open_rate"] == pytest.approx(2 / 3)  # email opens over email delivered
    assert stats["click_rate"] == pytest.approx(2 / 4)
    assert report["unsubscribed"] == 1
    assert report["not_arrived"] == 2
    assert {f["reason"]: f["count"] for f in report["failures"]} == {"Address does not exist": 1, "Has not connected Telegram": 1}
    assert report["outbox_url"] == f"/outbox/?campaign={sent_campaign.public_id}&status=failed"

    channels = {row["channel"]: row for row in report["channels"]}
    assert channels["email"]["counts"]["delivered"] == 3 and channels["email"]["counts"]["opened"] == 2
    assert channels["telegram"]["counts"]["delivered"] == 1 and "tapped the button" in channels["telegram"]["detail"]
    assert "opened" not in channels["telegram"]["detail"]  # Telegram has no open signal

    languages = {row["code"]: row for row in report["languages"]}
    assert languages["en"]["sent"] == 3 and languages["uk"]["sent"] == 2 and "fr" not in languages
    assert languages["uk"]["open_rate"] == 1 and languages["en"]["open_rate"] == pytest.approx(1 / 2)

    links = report["links"]
    assert [(l["url"], l["clicks"]) for l in links] == [(LINK, 2), ("https://maplegrove.example/drop-off", 1)]
    assert links[0]["label"] == "Donate a coat" and links[0]["share"] == pytest.approx(2 / 3)

    opens = report["opens"]
    assert len(opens["values"]) == 48 and opens["values"][0] == 1 and opens["values"][2] == 1 and opens["total"] == 2
    assert opens["svg"].startswith("<svg") and "<path" in opens["svg"] and len(opens["labels"]) == 5


def test_report_screen_and_export(sent_campaign):
    viewer = signed_in("viewer")
    html = viewer.get(f"/campaigns/{sent_campaign.public_id}/").content.decode()
    for text in ("Winter coat drive", "Delivered", "80.0% of 5 sent", "66.7%", "Opens in the first 48 hours", "By language",
                 "Links people clicked", "Donate a coat", "Did not arrive", "Address does not exist", "tapped the button",
                 f"/outbox/?campaign={sent_campaign.public_id}&amp;status=failed"):
        assert text in html, text
    assert "Use as a starting point" not in html  # a viewer cannot duplicate
    csv_text = viewer.get(f"/campaigns/{sent_campaign.public_id}/export/").content.decode()
    assert "olena@example.com" in csv_text and "Address does not exist" in csv_text
    row = next(r for r in reports.campaign_rows([sent_campaign]) if r["campaign"].pk == sent_campaign.pk)
    assert row["audience"] == 5 and row["open_rate"] == pytest.approx(2 / 3) and row["url"] == f"/campaigns/{sent_campaign.public_id}/"


def test_report_falls_back_to_totals_when_rows_are_gone(sent_campaign):
    from delivery import pipeline

    sent_campaign.totals = {**pipeline.campaign_totals(sent_campaign), **reports.report_extras(sent_campaign), "retained": True}
    sent_campaign.save()
    Message.objects.filter(campaign=sent_campaign).delete()
    report = reports.campaign_report(sent_campaign)
    assert not report["from_rows"] and report["has_data"]
    assert report["stats"]["sent"] == 5 and report["stats"]["delivered"] == 4 and report["stats"]["failed"] == 1
    assert report["stats"]["open_rate"] == pytest.approx(2 / 3)
    assert report["unsubscribed"] == 1 and report["links"][0]["clicks"] == 2 and report["not_arrived"] == 2
    html = signed_in("viewer").get(f"/campaigns/{sent_campaign.public_id}/").content.decode()
    assert "cleared after 180 days" in html and "80.0% of 5 sent" in html


def test_audience_counts_follow_subscriptions_and_reachability(org, campaign):
    newsletter = List.objects.get(slug="newsletter")
    bounced = add_person(newsletter, "bounced@example.com")
    Person.objects.filter(pk=bounced.pk).update(email_state="bounced")
    counted = checks.count_audience([newsletter], ["email"])
    assert counted["email"] == 3 and counted["people"] == 3 and counted["by_language"] == {"uk": 1, "fr": 1, "en": 1}
    assert checks.audience(campaign)["email"] == 3
    assert services.duplicate(campaign).channels == ["email"]


def test_send_now_through_the_real_pipeline(org, campaign):
    """End to end with only the mail server faked: fallback language, states and the report."""
    smtp = mock.MagicMock()
    smtp.sendmail.return_value = {}
    smtp.send_message.return_value = {}
    smtp.noop.return_value = (250, b"OK")
    smtp.__enter__.return_value = smtp
    with mock.patch("smtplib.SMTP", return_value=smtp), mock.patch("smtplib.SMTP_SSL", return_value=smtp):
        response = signed_in("editor").post(f"/campaigns/{campaign.public_id}/schedule/", {"when": "now"})
    assert response["Location"] == f"/campaigns/{campaign.public_id}/"
    campaign.refresh_from_db()
    assert campaign.state in ("sending", "sent") and campaign.started_at is not None
    rows = {m.person.email: m for m in Message.objects.filter(campaign=campaign).select_related("person")}
    assert set(rows) == {"olena@example.com", "marc@example.com", "dana@example.com"}
    assert rows["marc@example.com"].language == "en" and rows["marc@example.com"].used_fallback  # French is not written
    assert rows["olena@example.com"].language == "uk" and not rows["olena@example.com"].used_fallback
    html = signed_in("viewer").get(f"/campaigns/{campaign.public_id}/").content.decode()
    assert "of 3 sent" in html and "By language" in html
    assert reports.campaign_report(campaign)["stats"]["sent"] == 3
