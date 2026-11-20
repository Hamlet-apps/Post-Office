# Post Office — Build Spec

2026-11-20 · @Andrew

## 1. Summary

Post Office is a self-hosted messaging app for small organizations. It sends newsletters that staff write (campaigns) and messages that other apps trigger (app messages), by email, Telegram and push, in each reader's language. It is one building in the Hamlet suite, next to Town Hall (volunteers), Pavilion (restaurant), Bell Tower (tasks), Switchboard (workflows) and Gatehouse (sign-in).

It replaces listmonk for these organizations. The reasons to build it: multilingual content as a first-class feature, a visual email editor for both campaigns and app messages, more channels than email, and a modern UI.

Goals

- One install serves one organization. No multi-tenancy.

- Runs from docker compose up on a small server. No Node.js anywhere, at build time or run time.

- Every message a person can receive exists in every language the organization supports, with a defined fallback.

- A person gets each message on every channel they signed up for. There is no "first channel that works" logic.

- Staff with no coding skills can design emails by dragging blocks. People who know HTML can switch to HTML.

- Other Hamlet apps send their email and Telegram messages through Post Office, so staff can restyle them and see whether they went out.

Not in version 1

- SMS.

- A segment builder. Segments appear in the UI as a list only; see Open questions.

- Service (order, shift) push notifications. Each app sends those itself. Post Office only asks apps to send news and announcements.

- A/B tests, automation sequences, and a public template gallery.

How to read this spec

Sections 2 to 4 fix the stack and data model. Sections 5 to 13 describe behaviour, feature by feature. Section 14 is the HTTP API. Section 15 maps every screen to the design mockups. Section 16 gives the build order with a check for each step. Where this spec is silent, copy the mockups; where the mockups and this spec disagree, this spec wins.

## 2. Stack and deployment

Build it in Python with Django. Sending messages is almost entirely waiting on mail servers and web requests, so Python is fast enough; the queue design in section 7 matters far more than the language. Town Hall is also Django, which keeps the suite consistent.

| Layer | Choice | Notes |

| Language | Python 3.12 or newer |  |

| Web framework | Django 5.x | Server-rendered pages |

| Database | PostgreSQL 16 | Use JSONB for blocks, attributes and translations |

| Background jobs | Celery with Redis | One worker service, one beat (scheduler) service |

| Interactivity | HTMX, Alpine.js, SortableJS | Vendored as static files. No bundler, no npm |

| Styling | Hand-written CSS with custom properties | Tokens in section 15 |

| Email rendering | Django/Jinja templates on the server | Table layout, inline styles. Do not use MJML (needs Node) |

| Message templates | Jinja2 SandboxedEnvironment, autoescape on | Only variable output and for loops are allowed |

| HTTP client | httpx | Telegram, push endpoints, webhooks, command forwarding |

| Email sending | smtplib over a pooled connection per sender | Gmail and generic SMTP |

| Secrets at rest | Fernet encryption with a key from the environment | SMTP passwords, bot tokens, header values |

| API | Django REST Framework | Section 14 |

| Tests | pytest, pytest-django | Every acceptance check in section 16 has a test |

Services in docker-compose.yml

- web: gunicorn serving Django.

- worker: Celery worker.

- beat: Celery beat for scheduled campaigns, daily-limit resets and Telegram polling.

- db: PostgreSQL.

- redis: queue broker and rate-limit counters.

One image is used for web, worker and beat. Uploaded images go to a local media volume; S3-compatible storage is optional through django-storages.

Environment variables

SECRET_KEY, ENCRYPTION_KEY, DATABASE_URL, REDIS_URL, PUBLIC_URL (used in tracking, unsubscribe and signup links), TELEGRAM_MODE (polling or webhook, default polling so it works behind a home router), GATEHOUSE_ISSUER, GATEHOUSE_CLIENT_ID, GATEHOUSE_CLIENT_SECRET (all optional).

Project layout

Django apps: core (organization, languages, team, API keys), people (people, lists, subscriptions, consent), channels (senders, bots, push apps, webhooks), content (templates, blocks, rendering), campaigns, delivery (messages, queue, tracking), commands (Telegram commands), public (signup form, preferences page, tracking endpoints), api.

## 3. Concepts

These terms are used with one meaning throughout the spec and the UI.

| Term | Meaning |

| Person | Someone who can receive messages. Has a language, optional email, optional Telegram links, optional push memberships |

| Team member | Someone who signs in to Post Office. Separate from people |

| Building | Another Hamlet app connected to Post Office (Town Hall, Pavilion, Bell Tower) |

| Campaign | A message staff write and send to lists. Message type: newsletter |

| App message | A message a building triggers through the API, rendered from a template. Message type: service |

| Quick message | A one-off message staff send from a small dialog to people or lists |

| Message type | newsletter (needs consent, has unsubscribe) or service (about something the person did; still sent after they unsubscribe from newsletters) |

| Campaign list | A list people choose to join. Target of campaigns |

| App list | A private list a building keeps in step. Target of app messages and service quick messages only. Never offered on the signup form |

| Segment | A saved filter over people. Listed only in version 1 |

| Channel | email, telegram or push. Webhooks are an extra output, not a channel people subscribe to |

| Sender | An email account Post Office sends from. Exactly one has purpose app; any number have purpose newsletter |

| Bot | A Telegram bot. Each bot has its own connected people |

| Push app | A building that can deliver push notifications when Post Office asks it to |

| Notification | The short, plain-text form of a message used for Telegram and push. Telegram keeps the formatting; push strips it |

| Outbox | The log of every message, one row per person per channel |

| Command | A Telegram slash command that Post Office answers itself or passes to Switchboard or a web address |

## 4. Data model

All tables have id, created_at and updated_at. Public ids shown in URLs and the API are short prefixed strings (p_2041, cmp_88), not database keys. Fields marked encrypted use the Fernet key.

Core

| Model | Key fields |

| Organization (one row) | name, reply_to, mailing_address, timezone, missing_translation (fallback or hold), confirm_by_email (bool), keep_consent_proof (bool) |

| Language | code (BCP 47, e.g. en, uk, fr), name, is_default, position |

| TeamMember | email, name, role (owner, editor, viewer), password or gatehouse_subject |

| Building | name, kind, base_url, key_hash, key_prefix, last_seen_at |

| ApiKey | name, key_hash, key_prefix, scope (send, subscribe, all), last_used_at |

People

| Model | Key fields |

| Person | email (nullable, unique, case-insensitive), first_name, last_name, language, attributes (JSONB), email_state (ok, bounced, complained), source |

| List | name (translatable), slug, kind (campaign or app), building (nullable), on_signup_form (bool), description (translatable) |

| Subscription | person, list, channel, state (unconfirmed, on, off), consent. Unique on person, list, channel |

| ConsentRecord | person, lists, method (signup_form, import, api, staff), wording (exact text shown), language, ip, user_agent, link_params, given_at, confirmed_at |

| Segment | name, definition (JSONB, unused in version 1) |

Channels

| Model | Key fields |

| EmailSender | purpose (app or newsletter), from_name, from_email, kind (gmail or smtp), host, port, username, password (encrypted), daily_limit (nullable), per_second, is_default, state, last_error |

| TelegramBot | name, username, token (encrypted), is_default, webhook_secret, last_update_id |

| TelegramLink | person, bot, chat_id, username, linked_at, blocked_at. Unique on bot, chat |

| PushApp | building, label (e.g. "Offers and news"), endpoint, secret (encrypted), list (the campaign list it serves) |

| PushMember | person, push_app, external_id. Kept in step by the building |

| Webhook | name, url, secret (encrypted), enabled |

Content

| Model | Key fields |

| Template | key (e.g. pavilion.order_confirmation), kind (campaign or app), building (nullable), name, message_type, variables (JSONB schema with sample values), customised (bool) |

| Content | Owner is a Template, Campaign or QuickMessage. language, form (email or notification), subject, preview_text, mode (visual or html), blocks (JSONB), html, text (notification markdown), button_label, button_url, image. Unique on owner, language, form |

| Campaign | name, state (draft, scheduled, sending, sent, paused, cancelled), lists, channels, sender, bot, push_apps, scheduled_at, spread_minutes, created_by |

Delivery

| Model | Key fields |

| Message | person, source (campaign, building, quick), campaign or template, building, channel, language, used_fallback (bool), sender or bot or push_app, state (queued, sending, sent, opened, clicked, failed, skipped), error, attempts, send_after, idempotency_key (unique), subject_snapshot, data (JSONB), sent_at, opened_at, clicked_at |

| TrackedLink | campaign, url, token |

| LinkClick | message, link, at |

Commands

| Model | Key fields |

| Command | bot, name, descriptions (per language), target (switchboard or http), workflow_id, method, url, headers (list, values encrypted), body, reply_path, reply (answer or none), connected_only, enabled |

| BotText | bot, language, unknown_command, no_answer |

| CommandLog | bot, person (nullable), chat_id, text, command, target, result (answered, no_answer, unknown, error), duration_ms |

## 5. Languages

Language is part of the data model, not a workaround with one list per language.

- Setup. On first run the owner picks the languages the organization writes in and one default. Languages can be added, removed or re-defaulted later in Settings. Removing a language moves its people to the default.

- Person. Every person has exactly one language. It is set on the signup form, by the building that creates them, or by staff. New people with no language get the default.

- Content. Every campaign, template and quick message holds one Content row per language per form (email, notification). The editor shows one tab per language with a state: written, not written, default.

- Written means: for email, at least one block (or non-empty HTML) and a subject; for a notification, non-empty text.

- Fallback. When a person's language is not written, behaviour follows Organization.missing_translation. fallback: send the default language and set Message.used_fallback. hold: the campaign cannot be scheduled, and an app message for that person is sent in the default language anyway (service messages must not be dropped) with used_fallback set.

- Copy from default. Each empty language tab offers "Copy from English" (the default language's name) and "Start blank".

- Built-in wording. The footer, unsubscribe link, preferences page, signup form, confirmation email and bot replies ship translated through Django's translation system. Ship English, Ukrainian and French in version 1. A language with no shipped translation shows English for built-in wording and a warning in Settings.

- Translatable names. List names and descriptions, command descriptions and bot texts are stored as a JSON object keyed by language code, edited with the same language switcher.

- Reports. Campaign reports break down sent, opened and clicked by language.

## 6. Channels

The Channels screen has four sections: Email, Telegram, Push notifications, Webhooks. Each section lists cards and ends with an inline "add" card.

### Email senders

- There is exactly one sender with purpose app. All app messages use it. It can be replaced but not deleted while buildings are connected.

- There can be any number of newsletter senders, one marked default. A campaign picks one.

- Adding a sender: choose Gmail or Other mail server. Gmail asks for the name people see, the email address and an app password, and fills in smtp.gmail.com, port 587, STARTTLS. Other asks for host, port, username, password and TLS mode.

- "Test and save" opens an SMTP connection and authenticates. It saves only on success and shows the server's error otherwise.

- "Send a test" emails the signed-in team member.

- daily_limit is optional. When set, messages beyond the limit stay queued with send_after set to 00:05 the next day in the organization's time zone. Show "1,500 sent today, 918 waiting until tomorrow" on the card when this happens.

- per_second defaults to 5.

### Telegram bots

- Any number of bots, one default. Adding a bot takes the token from BotFather; Post Office calls getMe to verify it and reads the username.

- A person connects to a bot through a deep link: https://t.me/<bot_username>?start=<token>. The token is a signed, single-use value that names the person. The link appears in email footers (optional), on the preferences page and in "Get the connect link".

- /start <token> creates a TelegramLink. /start with no token offers the signup form link.

- When Telegram answers 403 (blocked by the user), set blocked_at and mark the message failed with a plain-language reason.

- Updates arrive by long polling (a beat task every 2 seconds per bot using getUpdates with last_update_id) or by webhook at /tg/<bot_id>/<webhook_secret>, chosen by TELEGRAM_MODE.

### Push notifications

Post Office never talks to phones. It asks a building to send, and only newsletters and announcements. Order and shift notifications stay inside each app and never pass through Post Office.

- Connecting an app takes its address. Post Office calls GET <address>/api/post-office to discover the app's name, kind, notification endpoint and the labels of its push audiences, then creates one PushApp per audience and one campaign list for each.

- The building keeps PushMember rows in step by calling the API (section 14) when someone allows or turns off notifications.

- To send, Post Office posts to the endpoint:

```
POST <endpoint>
X-PostOffice-Signature: sha256=<hmac of body with the shared secret>

{
  "audience": "offers-and-news",
  "idempotency_key": "cmp_88:push",
  "messages": [
    {"external_id": "cust_511", "title": "Maple Grove Community Centre", "body": "Holiday market on Sat, Dec 12...", "url": "https://maplegrove.example/calendar"}
  ]
}
```

- The building answers {"accepted": ["cust_511"], "rejected": []}. Accepted becomes sent; rejected becomes failed. Post Office batches up to 500 messages per request.

### Webhooks

A webhook has a name and an address. When enabled, Post Office posts a JSON copy of every campaign at send time (name, per-language subject and notification text, lists) signed the same way. It is for lobby displays and chat rooms, not for per-person delivery.

## 7. Sending pipeline

Every send, from a campaign, a building or a quick message, goes through the same seven steps. The rule that shapes it: a person gets the message on every channel they have switched on, and no message is ever sent twice.

- Resolve recipients. Collect the people on the chosen lists (or the one person named by an app message). Remove duplicates.

- Fan out by channel. For each person and each channel the send uses, create a Message when all of these hold: the person has a Subscription with state on for that list and channel; the channel is reachable (email present and email_state is ok; a TelegramLink to the chosen bot that is not blocked; a PushMember for the chosen push app). Otherwise create nothing, except when the person ends up with no message at all: then create one skipped message that records why.

- Pick the language. Use the person's language if written, else apply the fallback rule from section 5.

- Write rows. Insert messages with bulk_create in chunks of 1,000. idempotency_key is <source id>:<person id>:<channel>; the unique index makes a repeated fan-out harmless.

- Queue. One Celery task per chunk of 100 messages per channel. A campaign with spread_minutes staggers send_after evenly across that window.

- Send. The task locks its rows with SELECT ... FOR UPDATE SKIP LOCKED, sets sending, renders, sends, then sets sent or failed. Rendering happens here, per person, so placeholders are filled as late as possible.

- Record. Opens and clicks update the message later (below).

Limits. Each email sender has a token bucket in Redis for per_second and a daily counter for daily_limit. A task that cannot get a token re-queues itself with a short delay; a message over the daily limit gets send_after moved to the next day. Telegram is limited to 25 messages per second per bot. Push batches are sent one request at a time per app.

Retries. Temporary failures (connection errors, SMTP 4xx, Telegram 429 or 5xx, push endpoint 5xx or timeout) retry after 1, 5, 30 and 120 minutes, then fail. Permanent failures (SMTP 5xx, Telegram 400 or 403) fail at once with a reason written for staff, for example "Telegram says this person has blocked the bot".

Crash safety. A beat task every 5 minutes finds messages stuck in sending for more than 10 minutes. Service messages are re-queued once. Campaign messages are marked failed with the reason "interrupted, not retried" so nobody gets a newsletter twice; staff can retry them from the Outbox.

Bounces. In version 1 only errors returned during the SMTP conversation are seen. An SMTP 5xx for a recipient sets email_state to bounced and stops further email to that person until staff clear it. Reading bounce emails from the sender's inbox is an open question.

Email headers. Newsletters carry List-Unsubscribe (https link and mailto) and List-Unsubscribe-Post: List-Unsubscribe=One-Click. All email has a plain-text part generated from the blocks.

Tracking. Newsletter emails get a 1x1 open pixel at /t/o/<token>.gif and links rewritten to /t/c/<token>, which records the click and redirects. Telegram link buttons use the same click redirect. Service messages are not tracked. Tracking can be switched off in Settings.

Campaign state. scheduled becomes sending when fan-out starts and sent when no message is queued or sending. Pausing stops queued messages from being picked up; resuming continues without re-fanning out.

## 8. Consent and preferences

Newsletters need recorded consent and an unsubscribe link; service messages do not, but people can still choose their channels. The organization is in Canada, so design for CASL: consent is an action the person takes, and it is provable.

- Two message types. newsletter content goes only to people with an on subscription to a campaign list. service content (app messages, service quick messages) goes to people on app lists regardless of newsletter choices.

- Per list, per channel. A subscription is one person, one list, one channel. The preferences page is a grid of lists by channels. Unsubscribing from a newsletter by email leaves Telegram for that list untouched unless they untick it too.

- The consent box is never pre-ticked. Not on the form, not through a pre-filled link, not through the API. The person ticks it.

- Proof. Each consent creates a ConsentRecord with the exact wording shown, the language, time, IP address, user agent and any pre-filled link parameters. The person page shows it in a sentence.

- Confirm by email. When confirm_by_email is on, new email subscriptions start unconfirmed and one email with a confirm button is sent from the app sender. Confirming sets on and stamps confirmed_at. Unconfirmed subscriptions get no campaigns and are deleted after 14 days.

- Unsubscribe. Every newsletter email footer has "Unsubscribe" (one click, turns that list off for email, then shows the preferences page) and "Manage preferences". The footer also shows the organization's name and mailing address. The footer block cannot be removed in the editor.

- Telegram. /stop turns off every newsletter subscription on Telegram for that bot. /start after /stop restores nothing automatically; it shows the preferences link.

- Service messages. No unsubscribe link. Footer text says why they are receiving it. People may turn channels off for an app list on the preferences page.

- Unsubscribe from all news. One link on the preferences page sets every campaign-list subscription to off.

- Staff changes. Staff can turn subscriptions off for a person. Staff can turn a newsletter subscription on only by recording a consent with method staff and a required note saying how consent was given.

- Import. Importing people requires the importer to tick "these people agreed to receive this" and creates consent records with method import.

- Deleting a person removes their data but keeps a hashed email in a suppression table so a later import cannot silently resubscribe them.

## 9. Campaigns and the email editor

A campaign is made in two steps, Design then Audience and send, and has two forms of content per language: an email and a notification.

### Step 1: Design

The toolbar has language tabs, a form switch (Email or Notification) and, for email, a mode switch (Visual or HTML).

Email, Visual mode. Three columns: a block palette, the email canvas with subject and preview-text fields above it, and a panel for the selected block.

- Drag a block from the palette into the canvas, or press its + button to add it at the end. Drag blocks by their handle to reorder. Use SortableJS; the order is saved on drop.

- Clicking a block selects it. The panel edits its fields and offers Duplicate and Delete.

- The canvas has a Desktop and Phone preview width (600 and 380 pixels).

- The header (organization name or logo) and the footer are fixed blocks. The footer is locked, generated per language, and holds the reason line, mailing address, Unsubscribe and Manage preferences.

- Autosave on every change, debounced by 1 second. Show "Saved a moment ago".

Blocks are stored as a JSON list on Content.blocks:

```
[
  {"id": "b1", "type": "heading", "text": "December at Maple Grove", "align": "left"},
  {"id": "b2", "type": "text", "text": "Hi {first_name},\nDecember is a busy month."},
  {"id": "b3", "type": "image", "src": "/media/e/holiday-market.jpg", "alt": "Holiday market", "href": null},
  {"id": "b4", "type": "card", "title": "Holiday market", "text": "Saturday, December 12..."},
  {"id": "b5", "type": "button", "text": "See the full calendar", "href": "https://maplegrove.example/calendar"},
  {"id": "b6", "type": "divider"}
]
```

| Block type | Fields | Renders as |

| heading | text, align | One line, large, bold |

| text | text (line breaks kept; *bold*, _italic_, [label](url) allowed) | Paragraphs |

| image | src, alt (required), href | Full-width image, max 536 px |

| button | text, href | Rounded button in the organization's accent colour |

| card | title, text | Tinted box with a bold title |

| divider | none | Thin line |

| repeat (app templates only) | source, show | One row per item; see section 10 |

Placeholders. In block text a placeholder is stored as {name} and shown in the canvas as an orange tag with a readable label ("first name"). The panel has buttons to insert them. Campaigns offer first_name, last_name and preferences_link. An empty value renders as nothing and swallows one adjacent space or comma.

Email, HTML mode. Shows the HTML generated from the blocks in a code editor (a plain textarea with a monospace font is enough). Editing it sets Content.mode to html and stores the result in Content.html. Switching back to Visual asks: "Go back to blocks? Your HTML changes will be lost." In HTML mode placeholders are Jinja: {{ person.first_name }}. The footer is still appended by the server.

Rendering. A server-side function turns blocks into email HTML: a 600 px centred table, inline styles, system font stack, alt text on every image. The same function feeds the canvas preview, test sends and real sends, so they cannot drift apart.

Notification form. One text field with a small toolbar (bold, italic, link) that wraps the selection in Telegram-style marks: *bold*, _italic_, [label](url). A counter shows characters used against 4,096, Telegram's limit for a text message; with an image attached the limit shown is 1,024. Below it: an optional link button (label and address) and an optional image.

- Telegram sends the text with formatting, the image if present, and the link button as an inline keyboard button.

- Push sends the organization name as title, the text with marks stripped and line breaks collapsed, cut to 150 characters, and the button address as the tap target.

- Two previews sit beside the editor: a Telegram bubble and a push notification.

Missing language. A language tab with no content shows an empty state naming how many readers have that language, with "Copy from English" and "Start blank".

### Step 2: Audience and send

- Who gets it. Pick campaign lists (and later segments). Pick channels: Email with a sender, Telegram with a bot, and any push apps. Show reachable counts for each.

- Languages table. One row per language: readers, written or not for each form, and what they will get (their language, or the default as fallback).

- Before you send. A checklist computed on the server: languages written; unsubscribe footer present; sender healthy; links return a 2xx or 3xx status; everyone on the list has consent. Failures on languages follow the missing_translation setting; the others block scheduling.

- When. Send now or schedule a date and time in the organization's time zone. Option to spread the send over an hour.

- Test. Send a test to an address in one language or all of them.

### Report

For a sent campaign: delivered, opened, clicked and unsubscribed counts; opens over the first 48 hours; a by-language breakdown; a by-channel breakdown; links clicked; and a count of messages that did not arrive, grouped by reason. Telegram has no open signal; report delivered and button taps only.

## 10. App messages

Buildings send their email and Telegram messages through Post Office so staff can restyle them and see whether they went out. Each kind of message is a template the building registers and staff may customise.

Registering templates. When a building connects, and whenever it deploys, it sends its template definitions (section 14). Each has a key, a name, a description of when it fires, a variables schema with sample values, and default content per language. Post Office creates missing templates and updates the defaults of templates staff have not customised. Customised templates are never overwritten; "Reset to default" restores the building's version.

```
{
  "key": "pavilion.order_confirmation",
  "name": "Order confirmation",
  "fires": "When an order is placed",
  "variables": {
    "customer.first_name": {"label": "first name", "sample": "Olena"},
    "order.number": {"label": "order number", "sample": "#0142"},
    "order.ready_time": {"label": "ready time", "sample": "6:40 PM"},
    "order.total": {"label": "order total", "sample": "$23.50"},
    "order.link": {"label": "order link", "sample": "https://..."},
    "order.items": {"label": "order items", "list": {"name": {}, "quantity": {}, "price": {}, "notes": {}}}
  },
  "content": {"en": {"email": {"subject": "...", "blocks": []}, "notification": {"text": "..."}}}
}
```

Templates screen. Two parts: campaign templates as cards (starting points for campaigns, with "Start from blank"), and app messages grouped by building in tables showing channels, languages written, Default or Customised, and messages sent in 30 days.

Template editor. The same editor as campaigns (section 9) with three differences.

- The palette lists the building's variables under "From Pavilion". Clicking one inserts an orange tag at the cursor. Tags show the variable's label; "Sample order" preview mode fills them with sample values.

- A repeat block renders one row per item of a list variable. Its panel picks the list ("order items") and which fields each row shows.

- A panel shows the message type (service), the sender it goes out from, and which forms it has (email, Telegram). App messages have no push form.

Storage of placeholders. Visual mode stores {order.number} in block text. The renderer converts these to sandboxed Jinja lookups, with autoescape. HTML mode exposes Jinja directly: {{ order.number }} and {% for item in order.items %}...{% endfor %}. No other tags, filters or attribute access are allowed. An unknown variable renders as empty and logs a warning on the message.

Sending. A building posts to the API with the template key, the recipient, the data and an idempotency key (section 14). Post Office finds or creates the person, makes sure they are on the building's app list, and runs the pipeline in section 7 with message type service. Which forms go out is decided by the template (does it have email content, notification content) and by the person's channel choices for that app list.

Footer. Service emails end with the organization's name and address and one line saying why the person got it. No unsubscribe link.

Quick message. A dialog for staff: recipients (people or lists), channels (every channel they use, or one), one notification text per language, Send now. To app lists it is a service message; to campaign lists it is a newsletter and follows consent rules. It shows the fallback warning when a language is not written.

## 11. People, lists and public pages

### People screen

- A left panel groups lists: Everyone; Campaign lists (with "New list"); App lists (read-only, each showing its building's logo and a lock); Segments (listed, no builder).

- The table shows name and email, language, reachable channels, lists, status and join date. Filters: language, channel, status. Search by name or email. Paginated, 25 per page, with numbered pages.

- Import takes a CSV with email, first_name, last_name, language and any extra columns as attributes, a target campaign list, and the consent tick from section 8. Show a preview of the first 10 rows and a summary (new, updated, skipped) before committing.

### Lists

|  | Campaign list | App list |

| Created by | Staff, or a push app connection | A building, through the API |

| Joined by | The person, on the signup form, a building's opt-in tick box, or import | The building adding them |

| Receives | Campaigns and newsletter quick messages | App messages and service quick messages |

| On signup form | Optional per list | Never |

| Editable by staff | Name, description, members | Nothing; members are read-only |

Example for one organization: campaign lists Newsletter, Event updates and Pavilion news; app lists Volunteers (Town Hall), Customers (Pavilion) and Staff (Pavilion).

### Person page

Profile (name, email, language, extra details as key and value pairs), where they can be reached with the state of each channel, a grid of lists by channels with tick boxes, the consent sentence, a history of messages with their state, and two guarded actions: stop all newsletters, delete the person.

### Signup form

A public page at <PUBLIC_URL>/join. It shows the organization name, a language switch (starts on the browser's language when supported), first name, email, the campaign lists marked on_signup_form as tick boxes, an optional "also on Telegram" tick box, the consent tick box and Subscribe.

- The staff screen has toggles for which lists appear, whether to show the language switch, the Telegram box and the first-name field, and a live preview.

- After Subscribe: create or update the person, write subscriptions and the consent record, send the confirmation email if enabled, and show a thank-you page that offers the Telegram connect link when that box was ticked.

- Submitting an address that already exists does not reveal that; it sends that address an email with a link to their preferences.

- Rate-limit by IP and add a hidden honeypot field.

Pre-filled links. Staff build a one-off link that fills the form in for someone. Nothing is saved; the link is plain query parameters:

/join?name=Olena&email=olena.k%40example.com&lists=newsletter,events&telegram=1&lang=uk

lists holds list slugs. Everything except consent can be pre-filled. The builder is a small form on the staff screen (first name, email, a tick box per list, Telegram, language) with the resulting link and a Copy button, and the preview reflects it.

### Preferences page

A public page at /p/<signed token> reached from every footer. It shows a language switch that also changes the person's language, the grid of lists by channels (campaign lists under "News", app lists under "About things you signed up for"), "Unsubscribe from all news" and Done. Changes save as they are made. A note explains that in-app notifications about orders and shifts are controlled inside those apps.

## 12. Telegram commands

Post Office is a plain proxy for bot commands: it receives the command, passes it to Switchboard or a web address, and sends back whatever answer comes. It holds no workflow logic. The Commands screen is its own item in the side menu, with a bot picker at the top.

Handling an incoming message

- Ignore anything that does not start with /. (Free-text replies are out of scope.)

- Parse /name@BotName arg1 arg2 into command (lowercase, without the slash or bot suffix), text (the whole message) and arguments (the rest, as one string and as a list).

- Find the person through TelegramLink for this bot and chat. They may be unknown.

- If the command is built in, answer it. Otherwise look up an enabled Command for this bot.

- Not found: reply with the bot's unknown_command text in the person's language (default language if unknown).

- Found, connected_only and the person is unknown: reply asking them to press /start.

- Otherwise forward it, wait up to 10 seconds, and reply according to the command's settings.

- Write a CommandLog row in every case.

Built-in commands (always on, answered by Post Office, translated)

| Command | Does |

| /start | With a token: links this chat to the person. Without: sends the signup link |

| /stop | Turns off newsletter subscriptions on Telegram for this bot |

| /language | Shows the organization's languages as buttons and saves the choice |

| /help | Lists enabled commands with their descriptions in the person's language |

After any change to commands, call Telegram's setMyCommands once per language so the bot's menu matches.

A command's settings

- Name: lowercase letters, digits and underscores, up to 32 characters, unique per bot, not a built-in name.

- Description per language (shown in Telegram's menu).

- Pass it to: Switchboard or Web address. These are the only two targets.

- Reply: send back the answer, or no reply.

- Only for connected people (default on).

- Enabled.

Target: Switchboard. Pick a workflow from a list Post Office reads from Switchboard. Post Office posts the standard payload below to Switchboard's trigger address for that workflow. No other settings.

Target: Web address. A request builder:

- Method: GET, POST, PUT, PATCH or DELETE.

- Address.

- Headers: any number of name and value rows. Values are stored encrypted and shown masked after saving.

- Body: free text, hidden for GET and DELETE.

- Placeholders, inserted by buttons and written as {{name}}: command, text, arguments, person.id, person.first_name, person.language, person.telegram_id, bot. In the address they are URL-encoded; in a JSON body they are JSON-escaped.

- Reply field: a dot path into the JSON answer (for example reply or data.message). Empty means send the whole answer body as text.

- A live preview shows the exact request for a sample message, and "Send a test" fires it with the signed-in team member as the person.

Standard payload (Switchboard, and the suggested default body for a web address)

```
{
  "command": "shifts",
  "text": "/shifts saturday",
  "arguments": ["saturday"],
  "bot": "MapleGroveBot",
  "person": {"id": "p_2041", "first_name": "Taras", "language": "uk", "telegram_id": 48213377},
  "reply_url": "https://post.example/api/v1/commands/log_9912/reply"
}
```

Answers. A 2xx answer within 10 seconds is used. The reply text may use *bold*, _italic_ and [label](url). An empty answer sends nothing. A timeout, a connection error or a non-2xx status sends the bot's no_answer text and logs no_answer or error. A target can also answer later, once, by posting {"reply": "..."} to reply_url within 24 hours.

Safety. Refuse web addresses that resolve to loopback or link-local ranges unless ALLOW_PRIVATE_TARGETS=1 is set (self-hosters often call services on their own network, so make it a switch). Never include header values in logs. Limit each chat to 20 commands per minute.

## 13. Outbox, dashboard and settings

### Outbox

One row per Message, newest first, paginated. Columns: time, to, message, from (campaign or building, with logo), channel, language, status.

- A campaign fan-out is shown as one summary row ("Newsletter list, 2,377 people") until expanded; app messages and quick messages are shown individually.

- Clicking a campaign row opens its report (or its send screen if it is still scheduled). Clicking an app-message row opens that template in the template editor.

- Failed rows show the reason in plain words with "Try again" and "Open person".

- Filters: source (all, campaigns, each building), channel, status, day range. Search by person or message name.

- Four counters above the table: waiting to send, delivered today, failed today, scheduled.

- Keep message rows for 180 days, then delete them and keep only campaign totals.

### Dashboard

- Four number cards for the last 30 days: subscribers (with net change), messages sent across channels, open rate and click rate (newsletter email only, with change in points).

- An Engagement panel with a 7, 30 and 90 day switch and three charts sharing one time axis: opens (area line, with the busiest point labelled by the campaign that caused it), link clicks (bars) and new subscribers (bars). Daily buckets for 7 and 30 days, weekly for 90.

- "From the village today": messages each building sent today, an outbox state line, and delivered-of-total with a link to failures.

- Recent campaigns with status, language tags (a dashed tag marks a language not written) and rates.

- Audience: people by language, and people reachable per channel.

- Draw charts as inline SVG rendered on the server or with a few lines of vanilla JS. No chart library.

### Settings

One page with a section menu that jumps to each part.

| Section | Contents |

| Organisation | Name, reply-to address, mailing address (shown in newsletter footers), time zone |

| Languages | Supported languages with people counts, default, add and remove; what to do when a translation is missing; state of built-in wording per language |

| Connected buildings | Each building with its address, what it sends and when it last sent; connect a building; "Sign in with Gatehouse" switch |

| Consent | Confirm by email; keep proof of consent; the unsubscribe link (always on, shown as locked); tracking on or off |

| Team | Members with roles; invite by email |

| API keys | Keys for things outside Hamlet, with a scope; create and revoke. The full key is shown once |

### Roles

|  | Owner | Editor | Viewer |

| See dashboard, reports, outbox | Yes | Yes | Yes |

| Write and send campaigns, edit templates, manage people | Yes | Yes | No |

| Channels, commands, settings, team, API keys | Yes | No | No |

### Sign-in and first run

- Username and password, or "Continue with Gatehouse" (OpenID Connect) when the Gatehouse variables are set. A Gatehouse user is matched to a team member by email; unknown users are refused unless invited.

- First run, when no owner exists, is a four-step setup: your organisation, languages, channels (add the app sender; may be skipped), connect your buildings (may be skipped).

## 14. HTTP API

All endpoints live under /api/v1/, speak JSON, and authenticate with Authorization: Bearer <key>. Buildings use their building key; anything else uses an API key with a scope. Errors use {"error": {"code": "...", "message": "..."}} with a matching HTTP status.

| Method and path | Who | Does |

| POST /messages | Building, key with send | Send an app message from a template |

| GET /messages/{id} | Building, key with send | State of one send, per channel |

| PUT /templates | Building | Register or update the building's template definitions |

| PUT /lists/{slug} | Building | Create or update an app list or a building-owned campaign list |

| PUT /lists/{slug}/members/{external_id} | Building | Add or update a person on the list |

| DELETE /lists/{slug}/members/{external_id} | Building | Remove a person from the list |

| PUT /push-apps/{audience}/members/{external_id} | Building | Record that a person allowed push for this audience |

| DELETE /push-apps/{audience}/members/{external_id} | Building | Record that they turned it off |

| POST /subscribe | Key with subscribe | Same as the signup form, for a custom website form. Consent must be true and the wording shown must be sent |

| GET /people/{id}/preferences-link | Building | A signed preferences link for a person |

| POST /commands/{log_id}/reply | Switchboard or web target | A late answer to a command (section 12) |

| GET /health | Anyone | {"ok": true} for uptime monitors |

Sending an app message

```
POST /api/v1/messages

{
  "template": "pavilion.order_confirmation",
  "to": {"external_id": "cust_511", "email": "olena.k@example.com", "first_name": "Olena", "language": "uk"},
  "data": {
    "order": {"number": "#0142", "ready_time": "6:40 PM", "total": "$23.50", "link": "https://...",
              "items": [{"name": "Borscht", "quantity": 2, "price": "$9.00"}]}
  },
  "idempotency_key": "order-0142-confirmation"
}
```

Answer: 202 with {"id": "msg_...", "channels": ["email", "telegram"]}. Sending the same idempotency_key again returns the first answer and sends nothing. to is matched by external_id for that building first, then by email; unknown people are created. language updates the person only when they have none.

List members

```
PUT /api/v1/lists/volunteers/members/vol_88

{"email": "taras.melnyk@example.com", "first_name": "Taras", "language": "uk", "attributes": {"volunteer_since": "2024"}}
```

For a building-owned campaign list (for example Pavilion news), the body must also carry "consent": {"wording": "...", "given_at": "...", "ip": "..."}; without it the request is refused with consent_required.

What a building must expose for push (section 6): GET /api/post-office for discovery and POST <endpoint> to deliver. Both are part of the building, not of Post Office; document them in a short INTEGRATION.md in the repository.

Limits. 60 requests per second per key. POST /messages accepts one recipient per call; buildings send in a loop.

## 15. Screens and design

Every screen has an HTML mockup on the Post Office design canvas. Give the agent the exported mockup files along with this spec; they are the reference for layout, spacing and wording. All names and numbers in them are sample data for a fictional organization.

| Screen | Mockup file | URL in the app | Side menu |

| Dashboard | Main | / | Dashboard |

| Campaigns | Campaigns | /campaigns/ | Campaigns |

| Campaign editor | CampaignEditor | /campaigns/<id>/design/ | Campaigns |

| Audience and send | CampaignSend | /campaigns/<id>/send/ | Campaigns |

| Campaign report | CampaignReport | /campaigns/<id>/ | Campaigns |

| Templates | Templates | /templates/ | Templates |

| Template editor | TemplateEditor | /templates/<key>/ | Templates |

| People | People | /people/ | People |

| Person | Person | /people/<id>/ | People |

| Signup form settings | SignupForm | /people/signup-form/ | People |

| Outbox | Outbox | /outbox/ | Outbox |

| Channels | Channels | /channels/ | Channels |

| Commands | BotCommands | /commands/ | Commands |

| Settings | Settings | /settings/ | Settings (foot of menu) |

| Quick message | QuickMessage | dialog over any screen | none |

| First-run setup | Setup | /setup/ | none |

| Sign in | SignIn | /sign-in/ | none |

| Public signup form | preview inside SignupForm | /join | none |

| Preferences page | Preferences | /p/<token> | none |

The mockups' campaign editor already shows the drag, select, duplicate, delete, language-switch and HTML-view behaviour to copy. The template editor mockup is a static picture; build it with the same editor component.

Design tokens

| Token | Value | Used for |

| --teal | #0B7A85 | Side menu, selected states, primary charts, links |

| --teal-deep | #085E66 | Text on teal tint |

| --teal-tint | #DDF0F1 | Selected rows, tags, icon tiles |

| --peach | #FFD2B5 | The one primary action on a screen, with ink text |

| --peach-hover | #FFC39E | Primary action hover |

| --peach-deep | #94461A | Text on peach tint, placeholder tags |

| --peach-tint | #FFEADB | Warnings, placeholder tags |

| --peach-chart | #EE8A5A | Second chart series, dashed "missing" outlines |

| --ground | #F3F0EB | Page background, sunken fields |

| --surface | #FFFFFF | Panels |

| --ink | #1B1F2A | Text |

| --muted | #5B6170 | Secondary text |

| --ok / --ok-tint | #1A6B44 / #E3F3EA | Delivered, working |

| --danger / --danger-tint | #A3271C / #FCE8E6 | Failures, delete |

- Type. Headings and large numbers: Bricolage Grotesque 700. Everything else: Inter 400 to 700. Code and addresses: JetBrains Mono. Self-host the font files.

- Shape. Floating rounded panels on the ground colour, no hard borders. Radius 26 px for panels, 14 to 16 px for fields and buttons, pills for tabs and tags. One soft shadow for panels.

- Side menu. A 92 px teal rail with the logo on top, icon-over-label items, Settings and the avatar at the foot. It collapses to a top bar under 760 px.

- Icons. Inline stroke SVG, 24 px grid, round caps. The app icon is a sealed envelope: teal tile, peach envelope, teal seal with a peach star. Two colours, no gradients.

- Rules. Touch targets at least 40 px. Never signal state by colour alone; pair it with a word or icon. Text contrast at least 4.5 to 1. Honour prefers-reduced-motion. No hover-lift on cards. Every screen must work at phone width: menus stack, toolbars wrap, wide tables scroll inside their panel.

- Wording. Sentence case, plain verbs, no exclamation marks, no emoji. Say failures in words a non-technical person understands.

## 16. Build order and open questions

Build in seven steps. Each step ends with working software and the checks listed; write each check as an automated test.

| Step | Build | Done when |

| 1. Foundation | Project, Docker setup, sign-in, first-run setup, organisation, languages, team roles, the page shell and design tokens | docker compose up gives a sign-in page; setup creates an owner and three languages; a viewer cannot open Settings |

| 2. People | People, lists (both kinds), subscriptions, consent records, People and Person screens, import, pagination | Importing a CSV without the consent tick is refused; an app list cannot be edited by staff; a person's grid reflects their subscriptions |

| 3. Email and the pipeline | Email senders, the sending pipeline, Outbox, the buildings API (/messages, /templates, /lists), template editor in HTML mode | The same idempotency_key sent twice produces one email; a daily limit of 2 holds the third message until the next day; a killed worker does not resend a campaign message |

| 4. Campaigns | Campaign list, block editor (visual and HTML), language tabs and fallback, audience and send, scheduling, tracking, report, dashboard | A person with an unwritten language gets the default and used_fallback is set; with hold, the campaign cannot be scheduled; dragging a block changes the order in the sent email; unsubscribe turns off one list for email only |

| 5. Public pages | Signup form and its settings screen, pre-filled links, confirmation email, preferences page | A pre-filled link fills every field but never ticks consent; an unconfirmed subscriber gets no campaign; an existing address does not reveal itself |

| 6. Telegram | Bots, connect links, the notification form, Telegram delivery, commands (built-in, Switchboard, web address), Commands screen | A person with email and Telegram gets both; a blocked bot marks the link and fails the message with a readable reason; /help lists commands in the person's language; a target that does not answer in 10 seconds gets the fallback reply |

| 7. The rest | Push apps, webhooks, quick message, API keys, Gatehouse sign-in, the repeat block and visual mode for templates, retention job | A push send posts one signed batch to the app's endpoint; an API key with scope subscribe cannot send; the template editor renders one row per order item |

Throughout

- No Node.js in the repository: no package.json, no bundler. JavaScript libraries are vendored files.

- Every user-facing string goes through Django translations, with English, Ukrainian and French catalogues.

- Seed command manage.py seed_demo creates the fictional Maple Grove Community Centre data shown in the mockups. Tests and screenshots use it; no real organization's data appears anywhere in the repository.

- A README.md covers install, environment variables, connecting Gmail with an app password, and creating a Telegram bot.

Open questions (decide before the step named; until then follow the default)

- Segment builder: what can be filtered and how it is expressed. Default: segments are listed but cannot be created. Not in any step yet.

- Bounces that arrive later by email: read the sender's inbox over IMAP, or leave it to staff? Default: SMTP-time errors only. Step 3.

- Switchboard's trigger address and workflow list endpoint do not exist yet. Default: build against the payload in section 12 behind one adapter class. Step 6.

- One-click signup from a pre-filled link: is consent wording on the Subscribe button enough to drop the tick box? The button is enough, drop the box. Step 5.

- Gmail's daily sending caps: should the Gmail option suggest a daily_limit when a sender is added? Default: field is empty, with a hint. Step 3.

- Pavilion staff as a third audience: an app list only, or also a separate bot? Default: app list only; any bot can be chosen per send. Step 6.

- Should Bell Tower and Town Hall announcements to volunteers count as service or newsletter? Default: service when sent to an app list. Step 2.