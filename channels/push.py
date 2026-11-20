"""Push notifications (spec section 6).

Post Office never talks to phones. It asks a building to send:

    POST <endpoint>
    X-PostOffice-Signature: sha256=<hmac of body with the shared secret>

    {"audience": "offers-and-news", "idempotency_key": "cmp_88:push",
     "messages": [{"external_id": "cust_511", "title": "...", "body": "...", "url": "..."}]}

and the building answers {"accepted": ["cust_511"], "rejected": []}.

Connecting an app starts with discovery: GET <address>/api/post-office.
"""
import hashlib
import hmac
import json

import httpx
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext as _

from core.models import Building, Language

from .models import PushApp

TIMEOUT = 15
MAX_BATCH = 500


class PushError(Exception):
    """A push request that did not work. `temporary` means it is worth retrying."""

    def __init__(self, reason, temporary=False):
        super().__init__(reason)
        self.reason = reason
        self.temporary = temporary


def sign(secret, body):
    """The X-PostOffice-Signature value for a request body (bytes)."""
    if isinstance(body, str):
        body = body.encode()
    return "sha256=" + hmac.new((secret or "").encode(), body, hashlib.sha256).hexdigest()


def signed_post(url, payload, secret, timeout=TIMEOUT):
    """POST JSON signed with the shared secret. Returns the httpx response."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return httpx.post(url, content=body, timeout=timeout, headers={
        "Content-Type": "application/json",
        "X-PostOffice-Signature": sign(secret, body),
        "User-Agent": "PostOffice/1",
    })


def send_batch(push_app, messages, idempotency_key):
    """Ask the building to deliver up to 500 notifications in one request.

    messages: [{"external_id", "title", "body", "url"}]
    Returns (accepted, rejected) as sets of external ids. Raises PushError.
    """
    payload = {"audience": push_app.audience, "idempotency_key": idempotency_key, "messages": messages[:MAX_BATCH]}
    try:
        response = signed_post(push_app.endpoint, payload, push_app.secret)
    except httpx.TimeoutException as exc:
        raise PushError(_("The app did not answer in time"), temporary=True) from exc
    except httpx.HTTPError as exc:
        raise PushError(_("Could not reach the app"), temporary=True) from exc
    status = response.status_code
    if status >= 500:
        raise PushError(_("The app had a problem (it answered %(status)s)") % {"status": status}, temporary=True)
    if status == 429:
        raise PushError(_("The app asked us to slow down"), temporary=True)
    if status >= 400:
        if status in (401, 403):
            raise PushError(_("The app did not accept our signature. Check the shared secret."))
        raise PushError(_("The app refused the notifications (it answered %(status)s)") % {"status": status})
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    accepted = {str(x) for x in data.get("accepted") or []}
    rejected = {str(x) for x in data.get("rejected") or []}
    return accepted, rejected


# --------------------------------------------------------------------------- discovery

def normalize_address(address):
    address = (address or "").strip().rstrip("/")
    if address and "://" not in address:
        address = "https://" + address
    return address


def discover(address):
    """GET <address>/api/post-office.

    Returns {"name", "kind", "endpoint", "secret", "audiences": [{"audience", "label"}], "base_url"}.
    """
    base = normalize_address(address)
    if not base:
        raise PushError(_("Enter the app's address"))
    try:
        response = httpx.get(base + "/api/post-office", timeout=TIMEOUT, headers={"User-Agent": "PostOffice/1"})
    except httpx.HTTPError as exc:
        raise PushError(_("Could not reach an app at that address"), temporary=True) from exc
    if response.status_code != 200:
        raise PushError(_("That address did not answer like an app that works with Post Office (it answered %(status)s)")
                        % {"status": response.status_code})
    try:
        data = response.json()
    except ValueError as exc:
        raise PushError(_("That address did not answer like an app that works with Post Office")) from exc
    if not isinstance(data, dict):
        raise PushError(_("That address did not answer like an app that works with Post Office"))
    endpoint = data.get("notification_endpoint") or data.get("endpoint") or ""
    if endpoint.startswith("/"):
        endpoint = base + endpoint
    audiences = []
    for item in data.get("audiences") or data.get("push_audiences") or []:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or item.get("audience") or "").strip()
        slug = slugify(str(item.get("audience") or item.get("slug") or label))
        if slug:
            audiences.append({"audience": slug[:80], "label": label or slug})
    if not endpoint or not audiences:
        raise PushError(_("The app answered, but it did not say where to send notifications or who can get them"))
    kind = data.get("kind") or "other"
    if kind not in dict(Building.KIND_CHOICES):
        kind = "other"
    return {"name": str(data.get("name") or base.split("://", 1)[-1])[:120], "kind": kind, "endpoint": endpoint,
            "secret": str(data.get("secret") or ""), "audiences": audiences, "base_url": base}


def _unique_list_slug(base):
    from people.models import List

    slug = candidate = slugify(base)[:70] or "push"
    n = 2
    while List.objects.filter(slug=candidate).exists():
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


@transaction.atomic
def connect_app(address):
    """Discover an app and create one PushApp, and one campaign list, per audience.

    Returns (building, [PushApp, ...], created_count).
    """
    from people.models import List

    info = discover(address)
    building = (Building.objects.filter(base_url=info["base_url"]).first()
                or Building.objects.filter(kind=info["kind"]).exclude(kind="other").first()
                or Building.objects.filter(name__iexact=info["name"]).first())
    if building is None:
        building = Building.objects.create(name=info["name"], kind=info["kind"], base_url=info["base_url"])
    elif not building.base_url:
        building.base_url = info["base_url"]
        building.save(update_fields=["base_url", "updated_at"])
    apps, created_count = [], 0
    default_code = Language.default_code()
    for item in info["audiences"]:
        app = PushApp.objects.filter(building=building, audience=item["audience"]).first()
        if app is None:
            app = PushApp(building=building, audience=item["audience"])
            created_count += 1
        app.label = item["label"][:120]
        app.endpoint = info["endpoint"]
        if info["secret"]:
            app.secret = info["secret"]
        if app.list_id is None:
            app.list = List.objects.create(
                name={default_code: item["label"]}, slug=_unique_list_slug(f"{building.name}-{item['audience']}"),
                kind="campaign", building=building, on_signup_form=False,
                position=(List.objects.count() + 1),
            )
        app.save()
        apps.append(app)
    return building, apps, created_count
