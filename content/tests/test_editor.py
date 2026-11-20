import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from campaigns.models import Campaign
from content.editor import campaign_design, render_canvas
from content.models import Content, Template, get_content
from content.render import build_context, render_email, sample_context
from content.services import register_templates

pytestmark = pytest.mark.django_db

BLOCKS = [
    {"id": "b1", "type": "heading", "text": "December at Maple Grove", "align": "left"},
    {"id": "b2", "type": "text", "text": "Hi {first_name},\nDecember is a busy month."},
    {"id": "b3", "type": "button", "text": "See the full calendar", "href": "https://maplegrove.example/calendar"},
]


@pytest.fixture
def campaign():
    campaign = Campaign.objects.create(name="December newsletter")
    Content.objects.create(campaign=campaign, language="en", form="email", subject="December at Maple Grove", blocks=BLOCKS)
    return campaign


def save(client, owner, **data):
    url = reverse("content:editor_save", args=[owner.public_id])
    return client.post(url, data=json.dumps(data), content_type="application/json")


def test_dragging_a_block_changes_the_order_in_the_rendered_email(editor_client, campaign):
    new_order = [BLOCKS[2], BLOCKS[0], BLOCKS[1]]  # what the page posts after a drop
    response = save(editor_client, campaign, lang="en", form="email", subject="December at Maple Grove", blocks=new_order)
    data = response.json()
    assert data["ok"]
    canvas = data["canvas"]
    assert canvas.index('data-block-id="b3"') < canvas.index('data-block-id="b1"') < canvas.index('data-block-id="b2"')

    content = get_content(campaign, "en", "email")
    assert [b["id"] for b in content.blocks] == ["b3", "b1", "b2"]
    sent = render_email(content, build_context(None, {}, {"unsubscribe": "https://po/u/1"})).html
    assert sent.index("See the full calendar") < sent.index("December at Maple Grove") < sent.index("December is a busy month")
    assert "data-block-id" not in sent  # editor markers never reach a real email


def test_the_canvas_comes_from_the_renderer_with_tags_and_a_locked_footer(editor_client, campaign):
    data = save(editor_client, campaign, lang="en", form="email", blocks=BLOCKS).json()
    canvas = data["canvas"]
    assert canvas.startswith("<table") and canvas.endswith("</table>")
    assert "<tbody data-blocks>" in canvas
    assert 'class="po-var"' in canvas and ">first name<" in canvas  # {first_name} shown as an orange tag
    assert "data-header" in canvas and "Maple Grove Community Centre" in canvas
    assert "data-footer" in canvas and "Unsubscribe" in canvas
    assert next(l for l in data["langs"] if l["code"] == "en")["written"]
    assert not next(l for l in data["langs"] if l["code"] == "fr")["written"]


def test_a_repeat_block_renders_one_row_per_order_item(editor_client, pavilion, order_definition):
    register_templates(pavilion, [order_definition])
    template = Template.objects.get(key="pavilion.order_confirmation")
    content = get_content(template, "en", "email")

    # Placeholders: the row is shown once, as tags.
    tags = render_canvas(template, content)["canvas"]
    assert "One row for each of: order items" in tags and ">first name<" in tags

    # Sample data: one row per item the building sent as its sample.
    sample = render_canvas(template, content, sample=True)["canvas"]
    assert "Borscht" in sample and "Varenyky" in sample and "Kompot" in sample
    repeat_cell = sample.split('data-block-id="items"')[1].split("</table>")[0]
    assert repeat_cell.count("<tr>") == 3

    # And the same renderer with real data, as a send would call it.
    data = {"customer": {"first_name": "Olena"}, "order": {"number": "#0142", "items": [
        {"name": "Borscht", "quantity": 2, "price": "$9.00"}, {"name": "Kompot", "quantity": 1, "price": "$3.50"}]}}
    sent = render_email(content, build_context(None, data), message_type="service")
    assert sent.html.count("border-bottom:1px solid #F0ECE5") == 2 * 3  # two rows of three cells
    assert sent.subject == "Your order #0142 is in" and "Unsubscribe" not in sent.html

    # The page itself opens, with the building's variables in the palette.
    page = editor_client.get(reverse("content:template", args=[template.key]))
    assert page.status_code == 200
    body = page.content.decode()
    assert "From Pavilion" in body and "order number" in body and "Repeating list" in body
    assert "Service message" in body and "As a push notification" not in body
    assert sample_context(template.variables)["order"]["number"] == "#0142"


def test_saving_in_the_template_editor_marks_it_customised(editor_client, pavilion, order_definition):
    register_templates(pavilion, [order_definition])
    template = Template.objects.get(key="pavilion.order_confirmation")
    data = save(editor_client, template, lang="en", form="email", subject="Maple Grove has your order {order.number}").json()
    assert data["ok"] and data["customised"]
    template.refresh_from_db()
    assert template.customised
    assert get_content(template, "en", "email").subject == "Maple Grove has your order {order.number}"
    # A variable the building does not send is pointed out.
    data = save(editor_client, template, lang="en", form="email",
                blocks=[{"id": "t", "type": "text", "text": "Ready at {order.pickup_spot}"}]).json()
    assert any("order.pickup_spot" in w for w in data["warnings"])

    reset = editor_client.post(reverse("content:template_reset", args=[template.key]))
    assert reset.status_code == 302
    template.refresh_from_db()
    assert not template.customised
    assert get_content(template, "en", "email").subject == "Your order {order.number} is in"


def test_html_mode_refuses_if_tags_and_filters(editor_client, campaign):
    for bad in ("{% if person.first_name %}Hi{% endif %}", "<p>{{ person.first_name|upper }}</p>", "{{ person.__class__ }}"):
        data = save(editor_client, campaign, lang="en", form="email", html=bad).json()
        assert not data["ok"] and data["field"] == "html" and data["error"]
    content = get_content(campaign, "en", "email")
    assert content.mode == "visual" and content.html == ""  # nothing was stored

    good = "<p>Hi {{ person.first_name }}</p>{% for item in order.items %}<p>{{ item.name }}</p>{% endfor %}"
    data = save(editor_client, campaign, lang="en", form="email", html=good).json()
    assert data["ok"] and data["mode"] == "html"
    content.refresh_from_db()
    assert content.mode == "html" and content.html == good
    # HTML is previewed as a whole document (shown in a sandboxed frame), with the server's footer.
    assert data["canvas"] == "" and "Hi Olena" in data["doc"] and "Unsubscribe" in data["doc"]

    # "Go back to blocks" drops the HTML and keeps the blocks.
    data = save(editor_client, campaign, lang="en", form="email", mode="visual", blocks=BLOCKS).json()
    content.refresh_from_db()
    assert data["ok"] and content.mode == "visual" and content.html == "" and len(content.blocks) == 3


def test_the_html_view_starts_from_the_blocks(editor_client, campaign):
    page = editor_client.get(f"/templates/_editor/{campaign.public_id}/canvas/?lang=en&form=email")
    assert page.status_code == 200 and page.json()["ok"]
    request_page = _design_page(editor_client, campaign, "?view=html")
    state = _state(request_page)
    assert state["view"] == "html" and state["mode"] == "visual"
    assert "{{ person.first_name }}" in state["html"] and "December at Maple Grove" in state["html"]


def _design_page(client, campaign, query=""):
    """campaign_design is routed by the campaigns area; call it the way that route will."""
    from django.test import RequestFactory

    request = RequestFactory().get(f"/campaigns/{campaign.public_id}/design/{query}")
    request.user = client.user
    request.session = client.session
    response = campaign_design(request, campaign.public_id)
    assert response.status_code == 200
    return response.content.decode()


def _state(html):
    raw = html.split('<script id="editor-state" type="application/json">')[1].split("</script>")[0]
    return json.loads(raw)


def test_the_campaign_editor_page_shows_tabs_steps_and_the_empty_language(editor_client, campaign):
    from people.models import Person

    Person.objects.create(email="marc@example.com", first_name="Marc", language_id=None)
    french = Person.objects.create(email="chloe@example.com", first_name="Chloé")
    french.language = french.language.__class__.objects.get(code="fr")
    french.save()

    html = _design_page(editor_client, campaign)
    state = _state(html)
    assert state["kind"] == "campaign" and state["editable"] and not state["blank"]
    assert [p["name"] for p in state["placeholders"]] == ["first_name", "last_name", "preferences_link"]
    assert f"/campaigns/{campaign.public_id}/send/" in html and "Audience and send" in html
    assert 'data-block-id="b1"' in html and "Drag into the email, or press +" in html

    html = _design_page(editor_client, campaign, "?lang=fr")
    state = _state(html)
    assert state["blank"] and state["lang"] == "fr"
    assert "Français is not written yet" in html
    assert "1 reader has this language" in html
    assert "Copy from English" in html and "Start blank" in html

    copied = editor_client.post(reverse("content:editor_copy", args=[campaign.public_id]),
                                data=json.dumps({"lang": "fr", "form": "email"}), content_type="application/json").json()
    assert copied["ok"] and copied["copied"] == ["email"]
    assert get_content(campaign, "fr", "email").blocks == BLOCKS


def test_notification_previews_for_telegram_and_push(editor_client, campaign):
    text = "*December at Maple Grove*\nHoliday market on Sat, Dec 12. " + "Come by. " * 30
    data = save(editor_client, campaign, lang="en", form="notification", text=text,
                button_label="See the full calendar", button_url="https://maplegrove.example/calendar").json()
    assert data["ok"]
    note = data["note"]
    assert note["telegram_html"].startswith("<b>December at Maple Grove</b><br>")
    assert "*" not in note["push_body"] and len(note["push_body"]) <= 150 and note["push_body"].endswith("…")
    assert note["button_label"] == "See the full calendar" and not note["fallback"]
    # A language that is not written previews the default one and says so.
    data = editor_client.get(f"/templates/_editor/{campaign.public_id}/canvas/?lang=uk&form=notification").json()
    assert data["note"]["fallback"] and "December at Maple Grove" in data["note"]["telegram_html"]
    assert data["blank"]


def test_a_viewer_cannot_save_edits(viewer_client, campaign, pavilion, order_definition):
    register_templates(pavilion, [order_definition])
    template = Template.objects.get(key="pavilion.order_confirmation")
    for owner in (campaign, template):
        response = save(viewer_client, owner, lang="en", form="email", subject="Changed by a viewer", blocks=[])
        assert response.status_code == 403
        assert viewer_client.post(reverse("content:editor_copy", args=[owner.public_id]), data="{}",
                                  content_type="application/json").status_code == 403
        assert viewer_client.post(reverse("content:editor_upload", args=[owner.public_id])).status_code == 403
    assert get_content(campaign, "en", "email").subject == "December at Maple Grove"
    assert len(get_content(campaign, "en", "email").blocks) == 3
    assert viewer_client.post(reverse("content:template_reset", args=[template.key])).status_code == 403
    # A viewer may read a template, without the editing controls; the campaign editor is for editors.
    page = viewer_client.get(reverse("content:template", args=[template.key]))
    assert page.status_code == 200 and not _state(page.content.decode())["editable"]
    from django.core.exceptions import PermissionDenied
    from django.test import RequestFactory

    request = RequestFactory().get("/campaigns/x/design/")
    request.user = viewer_client.user
    with pytest.raises(PermissionDenied):
        campaign_design(request, campaign.public_id)


def test_a_sent_campaign_is_read_only(editor_client, campaign):
    campaign.state = "sent"
    campaign.save()
    response = save(editor_client, campaign, lang="en", form="email", subject="Too late", blocks=[])
    assert response.status_code == 409 and not response.json()["ok"]
    assert get_content(campaign, "en", "email").subject == "December at Maple Grove"
    html = _design_page(editor_client, campaign)
    assert not _state(html)["editable"] and "cannot be changed" not in html.split("editor-state")[0]
    assert "it can be read but not changed" in html


def test_uploading_a_picture_stores_it_in_media(editor_client, campaign, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
           b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
    url = reverse("content:editor_upload", args=[campaign.public_id])
    data = editor_client.post(url, {"file": SimpleUploadedFile("holiday market.gif", gif, content_type="image/gif")}).json()
    assert data["ok"] and data["url"].startswith("/media/e/") and data["url"].endswith(".gif")
    assert (tmp_path / data["url"][len("/media/"):]).exists()
    refused = editor_client.post(url, {"file": SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")})
    assert refused.status_code == 400 and not refused.json()["ok"]
    # An image block without a description is pointed out.
    blocks = [{"id": "i1", "type": "image", "src": data["url"], "alt": "", "href": None}]
    saved = save(editor_client, campaign, lang="en", form="email", blocks=blocks).json()
    assert saved["problems"] == {"i1": "alt"} and 'src="/media/e/' in saved["canvas"]
