# Connecting a building to Post Office

A building is another Hamlet app (Town Hall, Pavilion, Bell Tower). It sends its
email and Telegram messages through Post Office, keeps its lists in step, and,
if it has an app that shows push notifications, delivers the ones Post Office
asks it to. This page is everything a building has to do.

All calls go to `https://<post office>/api/v1/`, speak JSON and carry the
building's key:

    Authorization: Bearer po_bld_...

An owner creates the building and its key in Post Office under Settings. The key
is shown once. A key that is not a building key (an API key with a scope) can
only do what its scope allows: `send` may call `/messages`, `subscribe` may call
`/subscribe`, `all` may do both. Everything else below needs the building key.

Errors always look like this, with a matching HTTP status:

    {"error": {"code": "unknown_template", "message": "No app message template has this key. ..."}}

Limit: 60 requests per second per key. Beyond that the answer is `429` with the
code `throttled`; wait a second and continue.

Ids in addresses and answers are public ids (`p_2041`, `msg_17`), never database keys.

## 1. Register your templates

Do this when the building is first connected and again on every deploy. It is
safe to repeat.

    PUT /api/v1/templates

    {"templates": [
      {
        "key": "pavilion.order_confirmation",
        "name": "Order confirmation",
        "fires": "When an order is placed",
        "variables": {
          "customer.first_name": {"label": "first name", "sample": "Olena"},
          "order.number": {"label": "order number", "sample": "#0142"},
          "order.ready_time": {"label": "ready time", "sample": "6:40 PM"},
          "order.items": {"label": "order items", "list": {"name": {}, "quantity": {}, "price": {}}}
        },
        "content": {
          "en": {
            "email": {"subject": "Order {order.number} is in", "blocks": [{"type": "text", "text": "Ready at {order.ready_time}."}]},
            "notification": {"text": "Order {order.number} is in. Ready at {order.ready_time}."}
          }
        }
      }
    ]}

A bare list of definitions is accepted too. Answer: `{"created": 1, "updated": 0, "kept": 0}`.

Post Office creates templates it does not know and refreshes the ones staff
have not touched. A template staff have customised is never overwritten
(`kept`); your version stays available to them as "Reset to default".

Which forms a message has is decided here: a template with email content goes
out by email, one with notification content goes out on Telegram. App messages
have no push form.

## 2. Send an app message

One recipient per call; send in a loop.

    POST /api/v1/messages

    {
      "template": "pavilion.order_confirmation",
      "to": {"external_id": "cust_511", "email": "olena.k@example.com", "first_name": "Olena", "language": "uk"},
      "data": {"order": {"number": "#0142", "ready_time": "6:40 PM",
                         "items": [{"name": "Borscht", "quantity": 2, "price": "$9.00"}]}},
      "idempotency_key": "order-0142-confirmation"
    }

Answer: `202` with `{"id": "msg_17", "channels": ["email", "telegram"]}`.
`channels` lists where the message is going. An empty list means the person
could not be reached (no usable address, or they turned the channel off);
`GET /messages/{id}` says why.

- `idempotency_key` is required. Sending the same key again returns the first
  answer and sends nothing, so retry freely after a timeout.
- `to` is matched by your `external_id` first, then by `email`. Someone Post
  Office has not seen is created. `to` needs at least one of the two.
  `last_name` and `attributes` (an object) are accepted as well.
- `language` is used only when the person has no language yet. After that the
  person's own choice wins.
- The person is put on your app list automatically. If their language is not
  written, they get the default language: service messages are never dropped.

To see what happened:

    GET /api/v1/messages/msg_17

    {"id": "msg_17", "template": "pavilion.order_confirmation", "to": "p_2041", "created_at": "...",
     "channels": [{"channel": "email", "state": "sent", "language": "en", "used_fallback": true,
                   "error": "", "sent_at": "2026-11-20T02:40:11+00:00"}]}

States: `queued`, `sending`, `sent`, `opened`, `clicked`, `failed`, `skipped`.
`error` is a sentence written for staff. Temporary failures are retried after
1, 5, 30 and 120 minutes before a message becomes `failed`.

## 3. Keep your lists in step

An app list is private to your building (volunteers, customers). A campaign
list is one people choose to join (Pavilion news) and staff can send
newsletters to it.

Create or rename a list (safe to repeat; `kind` is `app` or `campaign` and
cannot change later; `name` and `description` may be text or an object keyed by
language code):

    PUT /api/v1/lists/volunteers

    {"name": {"en": "Volunteers", "uk": "Волонтери"}, "kind": "app"}

Add or update a person:

    PUT /api/v1/lists/volunteers/members/vol_88

    {"email": "taras.melnyk@example.com", "first_name": "Taras", "language": "uk",
     "attributes": {"volunteer_since": "2024"}}

Answer: `{"person": "p_2052", "list": "volunteers", "external_id": "vol_88", "channels": {"email": "on", "telegram": "on"}}`.
By default the person is switched on for email (when they have an address) and
Telegram; send `"channels": ["email"]` to choose. A channel the person turned
off themselves stays off unless you name it in `channels`.

Remove a person:

    DELETE /api/v1/lists/volunteers/members/vol_88      ->  204

### Campaign lists need consent

For a building-owned campaign list the body must also carry what the person
agreed to, because newsletters need provable consent:

    PUT /api/v1/lists/pavilion-news/members/cust_511

    {"email": "olena.k@example.com", "first_name": "Olena", "channels": ["email"],
     "consent": {"wording": "Send me Pavilion offers by email.", "given_at": "2026-11-02T18:40:00Z", "ip": "203.0.113.9"}}

Without `consent` (its `wording` and `given_at`) the call is refused with `422`
and the code `consent_required`. Never send consent the person did not give:
the box they tick must not be pre-ticked.

`DELETE` on a campaign list switches the person's subscriptions to that list
off rather than erasing them, so the record that they left is kept.

To let a person manage everything themselves, link them to their preferences
page (the id is your own id for the person, or a Post Office id):

    GET /api/v1/people/cust_511/preferences-link   ->  {"person": "p_2041", "url": "https://<post office>/p/..."}

## 4. Push notifications

Post Office never talks to phones. For newsletters and announcements only, it
asks your app to deliver. Order and shift notifications stay inside your app
and do not pass through Post Office. Your building has to expose two things.

### 4a. Discovery: `GET <your address>/api/post-office`

An owner types your address into Channels, "Connect an app". Post Office then
calls this once (and again whenever they reconnect) with no authentication:

    GET https://pavilion.maplegrove.example/api/post-office

    {
      "name": "Pavilion",
      "kind": "pavilion",
      "notification_endpoint": "https://pavilion.maplegrove.example/api/notifications",
      "audiences": [
        {"audience": "offers-and-news", "label": "Offers and news"}
      ],
      "secret": "a long random string"
    }

- `kind` is `town_hall`, `pavilion`, `bell_tower` or `other`.
- `notification_endpoint` may be a full address or a path starting with `/`.
- Each audience becomes one push app in Post Office and one campaign list with
  the same label. `audience` is a short slug you choose.
- `secret` is optional. It is the shared secret used to sign deliveries. If you
  leave it out, Post Office makes one and the owner copies it from the Channels
  screen ("Show the shared secret") into your configuration.

### 4b. Tell Post Office who allowed notifications

When someone allows notifications for an audience:

    PUT /api/v1/push-apps/offers-and-news/members/cust_511

    {"email": "olena.k@example.com", "first_name": "Olena", "language": "uk"}

The body may be `{}` for a person Post Office already knows by that id. It may
carry `"consent": {"wording": "...", "given_at": "...", "ip": "..."}`; when it
does not, Post Office records that they allowed notifications for that audience
in your app.

When they turn notifications off:

    DELETE /api/v1/push-apps/offers-and-news/members/cust_511     ->  204

### 4c. Delivery: `POST <notification_endpoint>`

When a campaign uses your audience, Post Office posts batches of up to 500
notifications, one request at a time:

    POST https://pavilion.maplegrove.example/api/notifications
    Content-Type: application/json
    X-PostOffice-Signature: sha256=<hex HMAC-SHA256 of the raw body with the shared secret>

    {
      "audience": "offers-and-news",
      "idempotency_key": "cmp_88:push:5121",
      "messages": [
        {"external_id": "cust_511", "title": "Maple Grove Community Centre",
         "body": "Holiday market on Sat, Dec 12...", "url": "https://maplegrove.example/calendar"}
      ]
    }

What you must do:

1. Check the signature against the raw request body before parsing it. In Python:

       expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
       if not hmac.compare_digest(expected, request.headers["X-PostOffice-Signature"]): return 401

2. Deliver each message to the devices of `external_id`. `body` is plain text of
   at most 150 characters; `url` may be empty.
3. Treat `idempotency_key` as "seen before": the same key again is the same
   batch being retried, so do not notify people twice. Each batch of a large
   campaign has its own key (the campaign, `push`, and a batch number).
4. Answer `200` with who you took and who you could not serve:

       {"accepted": ["cust_511"], "rejected": []}

   Accepted becomes "delivered" in the Outbox; rejected, or left out of both
   lists, becomes "failed".

A `5xx` answer, a `429` or no answer within 15 seconds is retried after 1, 5,
30 and 120 minutes. Any other `4xx` fails the batch at once (a `401` or `403`
tells staff to check the shared secret). "Send a test" on the Channels screen
posts a signed request with an empty `messages` list; answer it like any other.

## 5. Other endpoints

Signup from your own website form (needs a key with the `subscribe` scope, or
the building key). Consent must be literally `true` and the wording the person
saw must be sent; the answer is the same whether or not the address was known:

    POST /api/v1/subscribe

    {"email": "priya@example.com", "first_name": "Priya", "language": "en", "lists": ["newsletter"],
     "telegram": false, "consent": true, "wording": "Yes, send me the Maple Grove newsletter."}

A late answer to a Telegram command your address was asked to handle (once,
within 24 hours; the full address with its token arrives as `reply_url` in the
command payload):

    POST /api/v1/commands/log_91/reply?token=...

    {"text": "Your order #0142 is ready."}

Uptime check, no key needed:

    GET /api/v1/health   ->  {"ok": true}
