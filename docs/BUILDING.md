# Building Post Office: shared conventions

Read this before changing code. The product spec is `docs/SPEC.md` (where the mockups and
the spec disagree, the spec wins). The mockups are `design/mockups/*.dc.html`.

## Decisions that override the spec

- **Gatehouse is not built.** Sign-in is email and password only. Do not add a
  "Continue with Gatehouse" button or OpenID Connect code. Leave `TODO(gatehouse)` notes
  where it would go. In Settings, the "Sign in with Gatehouse" switch is shown disabled
  with a "not available yet" note.
- **Switchboard is not built.** Leave every Switchboard part out: commands have a single
  target, a web address. No workflow picker, no adapter, no Switchboard option in the UI.
  The standard payload is still the suggested default body for a web address.
- Team members sign in with their **email** (the mockup says "Username").

## Stack

Python 3.12, Django 5.2, DRF, Celery, httpx, Jinja2 (sandboxed), HTMX + Alpine.js + SortableJS
vendored in `static/js/vendor/`. **No Node.js, no package.json, no CDN links, no chart library.**
Local development and tests run on SQLite with Celery in eager mode (no Redis); production is
PostgreSQL + Redis. Code must work on both: use `JSONField`, avoid Postgres-only SQL, use the
Django cache (`django.core.cache.cache`) for counters and rate limits rather than a raw Redis client.

Run things with the project's virtualenv from the repository root:

    .venv/Scripts/python manage.py check
    .venv/Scripts/python -m pytest <app>

## What already exists (read it, build on it, do not rewrite it)

- `core/models.py`: `Organization.get()`, `Language`, `TeamMember` (the user model; roles owner,
  editor, viewer), `Building`, `ApiKey`, `PublicIdMixin` (`obj.public_id`,
  `Model.get_by_public_id("p_12")` raises 404), `tr(json_value, code)` for translatable JSON.
- `core/permissions.py`: `@role_required("viewer" | "editor" | "owner")` on **every** staff view.
  Viewer: see dashboard, reports, outbox. Editor: campaigns, templates, people. Owner: channels,
  commands, settings, team, API keys.
- `core/fields.py`: `EncryptedTextField`, `encrypt`, `decrypt`, `mask`.
- `core/templatetags/po.py`: `{% icon "name" [size] [stroke] %}` (files in
  `core/templates/icons/`; add a new `<name>.svg` holding only the inner SVG shapes if you need
  one), `{% app_icon %}`, `{% building_logo building 40 %}`, `|tr:"uk"`, `|pct`, `|get:key`,
  `{% qs page=2 %}`.
- Models for every app are written and migrated: `people`, `channels`, `content`, `campaigns`,
  `delivery`, `commands`. **Do not change models owned by another area.** If you truly need a
  field, add it to a model in an app you own with a new migration, and say so in your report.
- `people/services.py`: `find_or_create_person`, `record_consent`, `set_subscription`
  (raises `ConsentRequired`), `confirm_consent`, `unsubscribe_all_news`, `reachable_channels`,
  `subscription_grid`, `delete_person`. Use these instead of writing subscription rows directly.
- `people/tokens.py`: `preferences_url(person)`, `person_from_preferences_token`, `confirm_token`,
  `connect_url(person, bot)`, `person_from_connect_token`.
- `content/models.py`: `Template`, `QuickMessage`, `Content` (+ `is_written`), `get_content`,
  `written_languages`, `pick_content(owner, form, person_code) -> (content, used_fallback)`.
- `content/render.py`: `build_context`, `sample_context`, `render_email`, `render_notification`,
  `render_blocks`, `validate_jinja`, `blocks_to_editable_html`. The one renderer for the editor
  canvas, tests and real sends.
- `channels/email.py` (`open_connection`, `test_connection`, `describe_error`) and
  `channels/telegram.py` (`TelegramClient`, `TelegramError`).
- `api/auth.py` (`BearerKeyAuthentication`, `KeyPrincipal` with `.building`, `.api_key`,
  `.allows(scope)`) and `api/errors.py` (`ApiError`, `error_response`).
- Sign-in, sign-out, invites and the four-step setup: `core/views.py`.

## Pages

- Staff pages extend `templates/shell.html` and fill the blocks `page_title`, `title`,
  `subtitle`, `actions`, `content` (also `back`, `eyebrow`, `header`, `main_class`,
  `extra_css`, `extra_js`). The context processor supplies `org`, `org_languages`, `nav_items`.
- Public pages (signup, preferences) extend `templates/base.html` directly.
- Shared components are classes in `static/css/app.css` (`po-panel`, `po-card`, `po-btn` and
  its `--primary`/`--teal`/`--white`/`--ghost`/`--danger`/`--sm` variants, `po-iconbtn`,
  `po-field`, `po-input`, `po-select`, `po-textarea`, `po-search`, `po-check`, `po-switch`,
  `po-seg`, `po-pill`, `po-dot`, `po-tag`, `po-chip`, `po-var`, `po-table` inside
  `po-tablewrap`, `po-list`/`po-row`, `po-notice`, `po-empty`, `po-bar`, `dialog.po-dialog`,
  `po-pagination`, `po-stack`, `po-flex`, `po-cols`, `po-grid`, `po-h1`, `po-h2`, `po-h3`,
  `po-eyebrow`, `po-sub`, `po-muted`, `po-bignum`, `po-midnum`, `po-mono`, `sr-only`).
  **Do not edit `app.css`, `base.html`, `shell.html` or `app.js`.** Put screen-specific rules
  in your own `static/css/<area>.css` and load it through `{% block extra_css %}`.
- The mockups use inline styles. Copy their layout, spacing and wording, but express it with
  the shared classes plus your own CSS classes; keep inline styles for one-off values only.
  A mockup file is a few very long lines: read it in slices with Python
  (`open(path, encoding="utf-8").read()[a:b]`, markup starts after `</helmet>`), and look at the
  `data-dc-script` block at the end for sample data and behaviour.
- Behaviour: HTMX for server round trips (the CSRF header is already set on `<body>`), Alpine
  for local state. `static/js/app.js` gives you: `<dialog class="po-dialog" data-auto-open>`
  opens when swapped into `#po-dialog-slot`; `data-open-dialog="id"`; `data-close-dialog`;
  `data-copy="text"` / `data-copy-from="#sel"`; `data-confirm="..."` on forms; `poToast(text)`;
  and the `HX-Trigger: {"po-toast": "Saved"}` response header.
- Design rules: touch targets at least 40 px; never signal state by colour alone (pair it with
  a word or icon); text contrast at least 4.5:1; every screen works at phone width (menus stack,
  toolbars wrap, wide tables scroll inside their panel via `po-tablewrap`); no hover-lift.
  Use real `<button>`, `<a href>`, `<label>` + `<input>`.
- Wording: sentence case, plain verbs, no exclamation marks, no emoji. Say failures in words a
  non-technical person understands.
- Every user-facing string goes through Django translations (`{% trans %}`, `gettext`).
  Wording that readers (not staff) see must be rendered in the reader's language with
  `translation.override(code)`.
- URLs and the API use public ids (`p_2041`, `cmp_88`), never database keys.
- Sample data in tests is the fictional Maple Grove Community Centre. No real organization's data.

## Tests

pytest + pytest-django, in `<app>/tests/test_*.py`. Every "Done when" check in spec section 16
that falls in your area gets a test. Never call the network in tests: patch `smtplib`, `httpx`
and `TelegramClient`. `core/tests/test_foundation.py` shows the style.
