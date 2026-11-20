"""The dashboard and the server-drawn charts. Sample data: Maple Grove Community Centre."""
import json
import re
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from campaigns.models import Campaign
from content.models import Content, Template
from core import charts, dashboard
from core.models import Building, Language, Organization, TeamMember
from core.views import save_languages
from delivery.models import LinkClick, Message, TrackedLink
from people.models import List, Person
from people.services import record_consent, set_subscription

pytestmark = pytest.mark.django_db


@pytest.fixture
def org():
    cache.clear()
    save_languages(["en", "uk", "fr"], "en")
    org = Organization.get()
    org.name = "Maple Grove Community Centre"
    org.timezone = "America/Vancouver"
    org.setup_complete = True
    org.save()
    TeamMember.objects.create_user("andrew@maplegrove.example", "long password", role="owner", name="Andrew")
    return org


def signed_in(role="owner"):
    user = TeamMember.objects.filter(role=role).first() or TeamMember.objects.create_user(
        f"{role}@maplegrove.example", "long password", role=role, name=role.title())
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def village(org):
    """Six subscribers, one sent campaign with opens and clicks yesterday, and Town Hall messages today."""
    now = timezone.now()
    newsletter = List.objects.create(name={"en": "Newsletter"}, slug="newsletter", kind="campaign")
    people = []
    for i, code in enumerate(["en", "en", "en", "uk", "uk", "fr"]):
        person = Person.objects.create(email=f"neighbour{i}@example.com", first_name=f"Neighbour {i}", language=Language.objects.get(code=code))
        proof = record_consent(person, [newsletter], "signup_form", "Yes, send me the newsletter.", confirmed=True)
        set_subscription(person, newsletter, "email", True, consent=proof)
        people.append(person)
    campaign = Campaign.objects.create(name="Winter coat drive", state="sent", channels=["email"],
                                       started_at=now - timedelta(days=1), sent_at=now - timedelta(days=1))
    campaign.lists.add(newsletter)
    for code in ("en", "uk"):
        Content.objects.create(campaign=campaign, language=code, form="email", subject="Winter coat drive",
                               blocks=[{"id": "b1", "type": "text", "text": "Bring a coat."}])
    link = TrackedLink.objects.create(campaign=campaign, url="https://maplegrove.example/coat-drive")
    for i, person in enumerate(people):
        opened = i < 4
        message = Message.objects.create(
            person=person, source="campaign", message_type="newsletter", campaign=campaign, channel="email",
            language=person.language_code, state="clicked" if i == 0 else ("opened" if opened else "sent"),
            idempotency_key=f"{campaign.public_id}:{person.public_id}:email", sent_at=now - timedelta(days=1),
            opened_at=now - timedelta(days=1) + timedelta(minutes=5) if opened else None,
            clicked_at=now - timedelta(days=1) + timedelta(minutes=6) if i == 0 else None)
        if i == 0:
            LinkClick.objects.create(message=message, link=link)
            LinkClick.objects.filter(message=message).update(at=now - timedelta(days=1))
    Message.objects.filter(campaign=campaign).update(created_at=now - timedelta(days=1))
    Campaign.objects.create(name="Spring fair save the date")
    town_hall = Building.objects.create(name="Town Hall", kind="town_hall")
    reminders = Template.objects.create(key="town_hall.shift_reminder", kind="app", building=town_hall, name="Shift reminders")
    for i, person in enumerate(people[:3]):
        Message.objects.create(
            person=person, source="building", message_type="service", building=town_hall, template=reminders, channel="email",
            language="en", state="failed" if i == 2 else "sent", error="Mailbox full" if i == 2 else "",
            idempotency_key=f"shift:{person.public_id}", sent_at=None if i == 2 else now)
    return campaign


# --------------------------------------------------------------------------- the screen

def test_dashboard_renders_on_a_brand_new_install(org):
    response = signed_in().get("/")
    assert response.status_code == 200
    html = response.content.decode()
    assert re.search(r"Good (morning|afternoon|evening), Andrew", html)
    for text in ("Nothing has been sent yet", "Subscribers", "Messages sent", "Open rate", "Click rate", "Engagement",
                 "Nothing to chart for these days yet", "From the village today", "Outbox clear", "No building has sent anything today",
                 "Recent campaigns", "No campaigns yet", "Audience", "Nobody here yet", "New campaign", "Quick message",
                 'action="/search/"', 'hx-get="/templates/quick-message/"', "/people/signup-form/"):
        assert text in html, text
    assert html.count("<svg viewBox=\"0 0 760") == 3  # the three charts still draw their axes


def test_dashboard_renders_with_data(village):
    html = signed_in().get("/").content.decode()
    for text in ("No campaign is scheduled and the outbox is clear.", "Winter coat drive", "Spring fair save the date", "Shift reminders", "Town Hall · Email",
                 "2 of 3", "1 message failed. View in Outbox", "/outbox/?status=failed", "66.7%", "16.7%", "cp-lang--missing",
                 "Українська", "Reachable by channel", "6 this month"):
        assert text in html, text
    # The engagement charts are interactive: the page carries their data for static/js/charts.js.
    assert "data-po-charts" in html and 'id="ch-opens"' in html and "js/vendor/chart.umd.min.js" in html
    spec = json.loads(re.search(r'<script type="application/json" data-po-charts>(.*?)</script>', html, re.S).group(1))
    opens, clicks, subscribers = spec["charts"]
    assert spec["group"] == "engagement" and len(opens["labels"]) == len(opens["values"]) == 30
    assert max(opens["values"]) == 4 and opens["notes"][opens["peak"]] == "Winter coat drive"
    assert clicks["type"] == "bar" and subscribers["canvas"] == "ch-subscribers"
    assert "Nothing to chart" not in html


def test_a_viewer_sees_the_dashboard_without_the_editing_actions(village):
    html = signed_in("viewer").get("/").content.decode()
    assert "Engagement" in html and "Quick message" not in html and "New campaign" not in html


def test_the_range_switch_reloads_only_the_engagement_panel(village):
    client = signed_in()
    response = client.get("/?range=7", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="po-engagement")
    html = response.content.decode()
    assert response.status_code == 200 and "<html" not in html and html.lstrip().startswith("<section")
    assert 'id="po-engagement"' in html and 'href="?range=7" hx-get="/?range=7" hx-target="#po-engagement" hx-swap="outerHTML" aria-current="true"' in html
    assert "by week" in client.get("/?range=90", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="po-engagement").content.decode()
    assert client.get("/?range=nonsense").status_code == 200


# --------------------------------------------------------------------------- the numbers

def test_number_cards(village):
    org = Organization.get()
    cards = dashboard.number_cards(org)
    assert cards["subscribers"]["value"] == 6 and cards["subscribers"]["net"] == 6
    assert cards["sent"]["value"] == 8 and cards["sent"]["channels"] == "Email"
    assert cards["open_rate"]["value"] == pytest.approx(4 / 6) and cards["click_rate"]["value"] == pytest.approx(1 / 6)
    assert cards["open_rate"]["points"] is None  # nothing in the 30 days before to compare with
    assert cards["subscribers"]["spark"].startswith("<svg") and "polyline" in cards["subscribers"]["spark"]


def test_engagement_buckets_and_peak(village):
    org = Organization.get()
    week = dashboard.engagement(org, 7)
    assert len(week["opens"]["values"]) == 7 and week["opens"]["total"] == 4 and week["clicks"]["total"] == 1
    assert week["subscribers"]["total"] == 6 and len(week["x_labels"]) == 7
    assert week["opens"]["peak"]["name"] == "Winter coat drive" and week["opens"]["peak"]["value"] == 4
    month = dashboard.engagement(org, 30)
    assert len(month["opens"]["values"]) == 30 and len(month["x_labels"]) == 5 and not month["weekly"]
    quarter = dashboard.engagement(org, 90)
    assert len(quarter["opens"]["values"]) == 13 and quarter["weekly"] and quarter["opens"]["values"][-1] == 4
    assert quarter["opens"]["peak"]["name"] == "Winter coat drive"
    assert dashboard.engagement(org, 12)["days"] == 30


def test_village_and_audience(village):
    org = Organization.get()
    today = dashboard.village_today(org)
    assert today["total"] == 3 and today["delivered"] == 2 and today["failed"] == 1 and today["waiting"] == 0
    assert [(s["name"], s["count"], s["building"].name) for s in today["senders"]] == [("Shift reminders", 3, "Town Hall")]
    audience = dashboard.audience(org)
    assert audience["total"] == 6
    assert [(row["language"].code, row["count"]) for row in audience["languages"]] == [("en", 3), ("uk", 2), ("fr", 1)]
    assert {row["key"]: row["count"] for row in audience["channels"]} == {"email": 6, "telegram": 0, "push": 0}


# --------------------------------------------------------------------------- chart helpers

def test_line_and_area_paths():
    line, area, points = charts.line_path([0, 50, 100], width=760, height=190, vmax=100)
    assert line == "M0.0 188.0 L380.0 97.0 L760.0 6.0"
    assert area == line + " L760 190 L0 190 Z"
    assert points[0] == (0, 188) and points[-1][0] == 760
    assert charts.line_path([], 760, 190) == ("", "", [])
    flat, _area, _points = charts.line_path([7], 760, 190, vmax=10)
    assert flat.startswith("M0.0 ") and " L760.0 " in flat  # one value still draws a line
    assert re.fullmatch(r"M[\d.]+ [\d.]+( L[\d.]+ [\d.]+)+", charts.line_path([3, 1, 4, 1, 5], 760, 190)[0])


def test_bars_axis_and_peak():
    rects = charts.bar_rects([0, 5, 10], width=760, height=110, vmax=10)
    assert len(rects) == 3 and [r["value"] for r in rects] == [0, 5, 10]
    assert float(rects[0]["h"]) == 2.0  # a zero keeps a sliver
    assert float(rects[2]["h"]) == 104.0 and float(rects[2]["y"]) == 6.0
    assert all(0 <= float(r["x"]) and float(r["x"]) + float(r["w"]) <= 760 for r in rects)
    assert charts.bar_rects([], 760, 110) == []
    assert charts.nice_max([]) == 4 and charts.nice_max([0, 0]) == 4
    assert charts.nice_max([1310]) == 1500 and charts.nice_max([95]) == 150 and charts.nice_max([9]) == 10
    assert all(charts.nice_max([v]) >= v for v in (1, 7, 13, 99, 100, 101, 999, 4821, 123456))
    assert charts.axis_labels(1500, 3) == ["1,500", "1,000", "500", "0"]
    peak = charts.peak([1, 9, 3], height=190, vmax=10)
    assert peak["index"] == 1 and peak["value"] == 9 and peak["x"] == 50.0 and 0 < peak["y"] < 100
    assert charts.peak([0, 0, 0]) is None and charts.peak([]) is None
    assert charts.peak([9, 1, 1], vmax=10)["label_x"] == 14  # the label stays inside the panel


def test_whole_charts_are_safe_svg():
    area = charts.area_chart([1, 2, 3], label='Opens "today" <b>')
    assert area.startswith("<svg") and area.endswith("</svg>") and 'role="img"' in area
    assert "&quot;today&quot; &lt;b&gt;" in area and area.count("<path") == 2
    empty = charts.area_chart([], label="No opens")
    assert "<path" not in empty and empty.count("<line") == 4
    bars = charts.bar_chart([1, 2, 3], height=110, highlight=2, dim=0.78)
    assert bars.count("<rect") == 3 and bars.count('fill-opacity="0.78"') == 2
    spark = charts.sparkline([1, 3, 2, 8])
    xs_ys = re.search(r'points="([^"]+)"', spark).group(1).split()
    assert len(xs_ys) == 4 and all(0 <= float(p.split(",")[1]) <= 28 for p in xs_ys)
    assert "polyline" in charts.sparkline([]) and "polyline" in charts.sparkline([5])
