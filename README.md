# Post Office

A self-hosted messaging app for small organizations, and one building in the Hamlet suite.
It sends newsletters that staff write (campaigns) and messages that other apps trigger
(app messages), by email, Telegram and push, in each reader's language.

- The product spec is [docs/SPEC.md](docs/SPEC.md); the screen mockups are in `design/mockups/`.
- Conventions for working on the code are in [docs/BUILDING.md](docs/BUILDING.md).
- What another Hamlet app must do to send through Post Office is in [INTEGRATION.md](INTEGRATION.md).

One install serves one organization. There is no Node.js anywhere: the interface is
server-rendered Django with HTMX, Alpine.js and SortableJS vendored as static files.

## Not built yet

Two parts of the spec are deliberately left out because the apps they depend on do not exist yet.

### Gatehouse sign-in (to do)

Team members sign in with their **email and a password** only. "Continue with Gatehouse"
(OpenID Connect) is not implemented. What is in place for when Gatehouse exists:

- `GATEHOUSE_ISSUER`, `GATEHOUSE_CLIENT_ID` and `GATEHOUSE_CLIENT_SECRET` are read in
  `postoffice/settings.py` but unused.
- `TeamMember.gatehouse_subject` and `Organization.gatehouse_enabled` exist in the data model.
- The Settings screen shows the "Sign in with Gatehouse" switch disabled.
- `core/views.py` (`sign_in`) and `core/templates/core/sign_in.html` carry `TODO(gatehouse)`
  notes describing what to add: show the button when the variables are set, match the
  Gatehouse user to a team member by email, refuse users who were not invited.

Search the code for `TODO(gatehouse)` to find every place.

### Switchboard (to do)

Telegram commands can only be passed to a **web address**. The Switchboard target (pick a
workflow, post the standard payload to its trigger address) is left out, along with the
workflow list and any Switchboard settings. Search for `TODO(switchboard)`; the place to add
the second target is `commands/forward.py` and `Command.TARGET_CHOICES` in `commands/models.py`.

### Staff screens in Ukrainian and French (to do)

Everything **readers** see ships in English, Ukrainian and French: email footers, the signup
form, the confirmation email, the preferences page and the bot's replies
(`locale/reader_wording.py`). The **staff** screens are English only. Their strings are all
marked for translation and listed in `locale/uk` and `locale/fr`, but not translated yet, so
`core/middleware.py` keeps staff pages in English. Once the catalogues are complete, add
Django's `LocaleMiddleware` back in `postoffice/settings.py`.

## Install with Docker

You need Docker with the compose plugin.

```bash
cp .env.example .env
```

Edit `.env`: set `SECRET_KEY` to a long random string, generate an `ENCRYPTION_KEY` (the
file shows the command) and set `PUBLIC_URL` to the address people will use.

```bash
docker compose up -d
```

Open the address. The first visit starts a four-step setup: your organisation (this creates
the first owner), languages, the email account for app messages, and connecting your buildings.

Services: `web` (gunicorn), `worker` (Celery), `beat` (scheduled campaigns, daily-limit
resets, Telegram polling), `db` (PostgreSQL 16) and `redis`. Uploaded images go to the
`media` volume.

To try it with made-up data instead of an empty install:

```bash
docker compose exec web python manage.py seed_demo
```

This creates the fictional Maple Grove Community Centre from the mockups and a demo owner;
the command prints the email to sign in with, and the demo password is defined at the top of
`core/management/commands/seed_demo.py`. Change it after signing in. Do not seed a real install.

## Environment variables

| Variable | Needed | What it does |
| --- | --- | --- |
| `SECRET_KEY` | yes | Django's secret key |
| `ENCRYPTION_KEY` | yes | Fernet key that encrypts SMTP passwords, bot tokens and header values at rest. Losing it makes saved secrets unreadable |
| `DATABASE_URL` | set by compose | PostgreSQL address. Without it a local SQLite file is used |
| `REDIS_URL` | set by compose | Queue broker and rate-limit counters. Without it tasks run inline, which is only for development |
| `PUBLIC_URL` | yes | Used in tracking, unsubscribe, preferences and signup links |
| `TELEGRAM_MODE` | no | `polling` (default, works behind a home router) or `webhook` |
| `ALLOW_PRIVATE_TARGETS` | no | `1` lets Telegram commands call loopback and link-local addresses |
| `ALLOWED_HOSTS` | no | Comma-separated host names; defaults to any |
| `DEBUG` | no | `1` for development error pages |
| `MEDIA_ROOT` | no | Where uploaded images are stored |
| `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | no | Store uploads in S3-compatible storage instead |
| `GATEHOUSE_ISSUER`, `GATEHOUSE_CLIENT_ID`, `GATEHOUSE_CLIENT_SECRET` | no | Reserved for Gatehouse sign-in, which is not built yet |

## Connecting Gmail with an app password

Gmail will not accept your normal password from another app. You need an app password.

1. In your Google account, open **Security** and turn on **2-Step Verification**.
2. Open **App passwords** (search for it in the account settings), create one named
   "Post Office" and copy the 16 characters Google shows.
3. In Post Office, open **Channels**, add an email sender, choose **Gmail**, and enter the name
   people should see, the Gmail address and the app password. Post Office fills in
   `smtp.gmail.com`, port 587, STARTTLS.
4. Press **Test and save**. It saves only if Gmail accepts the sign-in.

Gmail limits how much an account may send per day (about 500 messages for a personal
account, more for Google Workspace). Set the sender's **daily limit** a little below your
account's cap; messages beyond it wait until the next day instead of failing.

## Creating a Telegram bot

1. In Telegram, start a chat with **@BotFather** and send `/newbot`.
2. Choose a name and a username ending in `bot`. BotFather replies with a token.
3. In Post Office, open **Channels**, add a Telegram bot and paste the token. Post Office
   checks it with Telegram and reads the bot's username.
4. People connect through a personal link (`https://t.me/<bot>?start=...`) shown on their
   preferences page and on the person page under "Get the connect link".

With `TELEGRAM_MODE=polling` nothing else is needed. With `webhook`, `PUBLIC_URL` must be an
`https` address Telegram can reach.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py compilemessages --ignore=.venv
.venv/Scripts/python manage.py seed_demo
.venv/Scripts/python manage.py runserver
```

(On macOS and Linux the interpreter is `.venv/bin/python`.) Without `DATABASE_URL` and
`REDIS_URL` this uses SQLite and runs background tasks inline, so nothing else is required.

```bash
.venv/Scripts/python -m pytest
```

Layout: `core` (organization, languages, team, API keys, settings, dashboard), `people`
(people, lists, subscriptions, consent), `channels` (senders, bots, push apps, webhooks),
`content` (templates, blocks, rendering, the editor), `campaigns`, `delivery` (messages,
queue, tracking, outbox), `commands` (Telegram commands), `public` (signup form,
preferences page), `api` (the HTTP API under `/api/v1/`).
