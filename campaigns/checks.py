"""The "Before you send" checklist (spec section 9, step 2).

    checklist(campaign)     -> list[Check], computed on the server
    can_schedule(campaign)  -> (ok, [reasons]) and is what the schedule view enforces

Rules: the default language must always be written for every form the chosen
channels need. Other languages follow Organization.missing_translation
("fallback": a warning that does not block; "hold": blocks). A missing list or
channel, a missing unsubscribe footer, a sender that is not working, a broken
link and missing consent all block scheduling.
"""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.core.cache import cache
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from content.models import Content
from core.models import Language, Organization
from people.models import Person, Subscription

LINK_TIMEOUT = 4.0
LINK_CACHE_SECONDS = 300
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'(=>])\bhttps?://[^\s<>\"')\]]+")
_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["'](https?://[^"']+)["']""", re.I)
_UNSUBSCRIBE_MARK = "https://unsubscribe.invalid/check"


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""
    blocking: bool = True  # a failed check with blocking=False is a warning

    @property
    def state(self):
        return "ok" if self.ok else ("blocked" if self.blocking else "warning")


# --------------------------------------------------------------------------- what the campaign needs

def needed_forms(campaign):
    """The content forms the chosen channels use: email, and one notification for Telegram and push."""
    channels = campaign.channels or []
    forms = []
    if "email" in channels:
        forms.append("email")
    if "telegram" in channels or "push" in channels:
        forms.append("notification")
    return forms


def written_map(campaign):
    """{form: set of language codes written} for both forms, in one query."""
    out = {"email": set(), "notification": set()}
    for content in Content.objects.filter(campaign=campaign):
        if content.is_written:
            out.setdefault(content.form, set()).add(content.language)
    return out


def language_rows(campaign, by_language=None):
    """One row per organization language for the Languages table."""
    by_language = by_language or {}
    forms = needed_forms(campaign)
    written = written_map(campaign)
    default = Language.default()
    rows = []
    for lang in Language.objects.all():
        cells = {form: lang.code in written[form] for form in forms}
        complete = all(cells.values()) if forms else False
        rows.append({
            "language": lang,
            "is_default": bool(default and lang.pk == default.pk),
            "readers": by_language.get(lang.code, 0),
            "email": cells.get("email"),
            "notification": cells.get("notification"),
            "complete": complete,
            "gets": lang if complete or not default else default,
            "fallback": bool(forms) and not complete and bool(default) and lang.pk != default.pk,
        })
    return rows


# --------------------------------------------------------------------------- links

def collect_links(campaign):
    """Every web address the campaign points to, as [(url, label)] without repeats.

    Looks in button, image, text, heading and card blocks, in HTML-mode email,
    and in the notification text and its button.
    """
    found = {}

    def add(url, label=""):
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")) or "{" in url:
            return  # placeholders such as {preferences_link} are filled in per person
        url = url.rstrip(".,;")
        if url not in found or (label and not found[url]):
            found[url] = (label or "").strip()

    def from_text(text):
        text = text or ""
        for label, url in _MD_LINK_RE.findall(text):
            add(url, label)
        for url in _BARE_URL_RE.findall(_MD_LINK_RE.sub(" ", text)):
            add(url)

    for content in Content.objects.filter(campaign=campaign).order_by("form", "language"):
        if content.form == "email":
            if content.mode == "html":
                for url in _HREF_RE.findall(content.html or ""):
                    add(url)
                continue
            for block in content.blocks or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "button":
                    add(block.get("href"), block.get("text"))
                elif kind == "image":
                    add(block.get("href"), block.get("alt"))
                    add(block.get("src"), block.get("alt"))
                for key in ("text", "title"):
                    if kind != "button" and isinstance(block.get(key), str):
                        from_text(block[key])
        else:
            from_text(content.text)
            add(content.button_url, content.button_label)
    return list(found.items())


def _probe(url):
    """Return "" when the address answers with a 2xx or 3xx status, else a short code for the reason."""
    import httpx

    headers = {"User-Agent": "PostOffice link check"}
    try:
        response = httpx.head(url, timeout=LINK_TIMEOUT, follow_redirects=False, headers=headers)
        if response.status_code >= 400:  # some sites refuse HEAD; ask again the ordinary way
            response = httpx.get(url, timeout=LINK_TIMEOUT, follow_redirects=False, headers=headers)
    except httpx.TimeoutException:
        return "timeout"
    except Exception:
        return "unreachable"
    if 200 <= response.status_code < 400:
        return ""
    return str(response.status_code)


def _reason(code):
    """The reason in words a non-technical person understands."""
    if code == "timeout":
        return _("did not answer in time")
    if code == "unreachable":
        return _("could not be reached")
    if code == "404":
        return _("page not found")
    return _("answered with error %(status)s") % {"status": code}


def check_links(urls, use_cache=True):
    """{url: reason} for the addresses that do not work. Results are remembered for a few minutes."""
    urls = list(dict.fromkeys(urls))
    results, todo = {}, []
    for url in urls:
        key = "linkcheck:" + hashlib.sha256(url.encode()).hexdigest()
        hit = cache.get(key) if use_cache else None
        if hit is None:
            todo.append((url, key))
        else:
            results[url] = hit["code"]
    if todo:
        if len(todo) == 1:
            codes = [_probe(todo[0][0])]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                codes = list(pool.map(_probe, [url for url, _key in todo]))
        for (url, key), code in zip(todo, codes):
            results[url] = code
            cache.set(key, {"code": code}, LINK_CACHE_SECONDS)
    return {url: _reason(code) for url, code in results.items() if code}


# --------------------------------------------------------------------------- consent and footer

def people_without_consent(campaign):
    """How many people would get this campaign without a consent record behind their subscription."""
    lists = list(campaign.lists.all())
    channels = campaign.channels or []
    if not lists or not channels:
        return 0
    return (
        Subscription.objects.filter(list__in=lists, channel__in=channels, state="on", consent__isnull=True)
        .values("person_id").distinct().count()
    )


def footer_missing_in(campaign, org=None):
    """Language codes whose rendered email carries no unsubscribe link."""
    from content.render import build_context, render_email

    org = org or Organization.get()
    missing = []
    context = build_context(None, {}, {"unsubscribe": _UNSUBSCRIBE_MARK, "preferences": "https://unsubscribe.invalid/p"})
    for content in Content.objects.filter(campaign=campaign, form="email"):
        if not content.is_written:
            continue
        try:
            rendered = render_email(content, dict(context), message_type="newsletter", org=org)
        except Exception:
            missing.append(content.language)
            continue
        if _UNSUBSCRIBE_MARK not in rendered.html:
            missing.append(content.language)
    return missing


# --------------------------------------------------------------------------- the checklist

def _names(languages):
    return ", ".join(lang.english_name or lang.name for lang in languages)


def checklist(campaign, links=True, by_language=None):
    """The list shown under "Before you send". Set links=False to skip the network check."""
    org = Organization.get()
    by_language = by_language or {}
    checks = []
    lists = list(campaign.lists.all())
    channels = [c for c in (campaign.channels or []) if c in ("email", "telegram", "push")]
    forms = needed_forms(campaign)
    languages = list(Language.objects.all())
    default = Language.default()
    written = written_map(campaign)

    # Audience
    if lists:
        checks.append(Check("lists", ngettext("Sending to %(n)s list", "Sending to %(n)s lists", len(lists)) % {"n": len(lists)}, True))
    else:
        checks.append(Check("lists", _("Choose at least one list"), False, _("Nobody would get this campaign yet.")))
    if not channels:
        checks.append(Check("channels", _("Choose at least one channel"), False, _("Pick email, Telegram or a push app.")))

    # Languages
    if forms and default:
        form_names = {"email": _("email"), "notification": _("notification")}
        default_missing = [form for form in forms if default.code not in written[form]]
        complete = [lang for lang in languages if all(lang.code in written[form] for form in forms)]
        if default_missing:
            checks.append(Check(
                "default_language",
                _("%(language)s, your default language, is not written yet") % {"language": default.english_name or default.name},
                False,
                _("Write the %(forms)s in %(language)s. People whose language is missing get the default.") % {
                    "forms": " and ".join(str(form_names[f]) for f in default_missing),
                    "language": default.english_name or default.name,
                },
            ))
        else:
            checks.append(Check(
                "languages_written",
                _("Subject and content written in %(done)s of %(all)s languages") % {"done": len(complete), "all": len(languages)},
                True,
            ))
        hold = org.missing_translation == "hold"
        for lang in languages:
            if lang.pk == default.pk or lang in complete:
                continue
            name = lang.english_name or lang.name
            readers = by_language.get(lang.code)
            if hold:
                label = _("%(language)s is missing. Write it before sending.") % {"language": name}
                detail = _("Your settings hold a campaign until every language is written.")
            elif default_missing:
                label = _("%(language)s is missing.") % {"language": name}
                detail = ""
            elif readers:
                label = ngettext(
                    "%(language)s is missing. %(n)s reader will get %(default)s.",
                    "%(language)s is missing. %(n)s readers will get %(default)s.", readers,
                ) % {"language": name, "n": f"{readers:,}", "default": default.english_name or default.name}
                detail = ""
            else:
                label = _("%(language)s is missing. Its readers will get %(default)s.") % {
                    "language": name, "default": default.english_name or default.name}
                detail = ""
            checks.append(Check(f"language_{lang.code}", label, False, detail, blocking=hold))

    # Unsubscribe footer
    if "email" in channels:
        missing = footer_missing_in(campaign, org)
        if missing:
            checks.append(Check(
                "unsubscribe", _("The unsubscribe link is missing from the footer"), False,
                _("Missing in: %(languages)s") % {"languages": ", ".join(code.upper() for code in missing)},
            ))
        else:
            checks.append(Check("unsubscribe", _("Unsubscribe link is in the footer in every language"), True))

    # Sender, bot, push apps
    if "email" in channels:
        sender = campaign.sender
        if sender is None:
            checks.append(Check("sender", _("Choose an email sender"), False, _("Add one in Channels if there is none to pick.")))
        elif sender.state != "ok":
            reason = sender.last_error or (
                _("It has not been tested yet. Send a test from Channels first.") if sender.state == "untested"
                else _("The last connection test failed.")
            )
            checks.append(Check("sender", _("%(address)s is not working") % {"address": sender.from_email}, False, reason))
        else:
            checks.append(Check("sender", _("Sending from %(address)s") % {"address": sender.from_email}, True))
    if "telegram" in channels and campaign.bot is None:
        checks.append(Check("bot", _("Choose a Telegram bot"), False, _("Add one in Channels if there is none to pick.")))
    if "push" in channels and campaign.pk and not campaign.push_apps.exists():
        checks.append(Check("push", _("Choose a push app"), False))

    # Consent
    if lists and channels:
        without = people_without_consent(campaign)
        if without:
            checks.append(Check(
                "consent",
                ngettext("%(n)s person has no recorded consent", "%(n)s people have no recorded consent", without) % {"n": f"{without:,}"},
                False,
                _("Newsletters need consent. Record it on the person's page, or take them off the list."),
            ))
        else:
            checks.append(Check("consent", _("Everyone on the list agreed to get this"), True))

    # Links
    if links and forms:
        found = collect_links(campaign)
        if found:
            broken = check_links([url for url, _label in found])
            if broken:
                detail = "; ".join(f"{url} ({reason})" for url, reason in list(broken.items())[:5])
                checks.append(Check(
                    "links",
                    ngettext("%(n)s link does not work", "%(n)s links do not work", len(broken)) % {"n": len(broken)},
                    False, detail,
                ))
            else:
                checks.append(Check(
                    "links", ngettext("The link works", "All %(n)s links work", len(found)) % {"n": len(found)}, True))
        else:
            checks.append(Check("links", _("No links to check"), True))
    return checks


def can_schedule(campaign):
    """Whether the campaign may be scheduled or sent, and the reasons when it may not."""
    if campaign.state not in ("draft", "scheduled"):
        return False, [_("This campaign has already been sent.")]
    reasons = [c.label for c in checklist(campaign) if not c.ok and c.blocking]
    return (not reasons), reasons


# --------------------------------------------------------------------------- audience

def audience(campaign, channels=None, bot=None):
    """Reachable counts for the campaign's current choices.

    Uses delivery.pipeline.audience when the pipeline is installed, so the
    numbers match what a send would do; otherwise counts the same way here.
    Pass `channels` (and `bot`) to ask what other channels could reach.
    Always returns the keys people, email, telegram, push, by_language, without_consent.
    """
    lists = list(campaign.lists.all())
    channels = list(campaign.channels or []) if channels is None else list(channels)
    bot = bot or campaign.bot
    push_apps = list(campaign.push_apps.all()) if campaign.pk else []
    result = None
    try:
        from delivery import pipeline

        result = pipeline.audience(lists, channels, sender=campaign.sender, bot=bot, push_apps=push_apps)
    except (ImportError, AttributeError):
        result = None
    if not isinstance(result, dict):
        result = count_audience(lists, channels, bot=bot, push_apps=push_apps)
    out = {"people": 0, "email": 0, "telegram": 0, "push": 0, "by_language": {}, "without_consent": 0}
    out.update(result)
    return out


def count_audience(lists, channels, bot=None, push_apps=()):
    """The pipeline's rule (spec section 7): a subscription that is on, and a channel that can be reached."""
    empty = {"people": 0, "email": 0, "telegram": 0, "push": 0, "by_language": {}, "without_consent": 0}
    if not lists:
        return empty

    def subscribed(channel):
        return Person.objects.filter(subscriptions__list__in=lists, subscriptions__channel=channel, subscriptions__state="on")

    email = subscribed("email").filter(email__isnull=False, email_state="ok").exclude(email="").distinct()
    telegram = Person.objects.none()
    if bot is not None:
        telegram = subscribed("telegram").filter(telegram_links__bot=bot, telegram_links__blocked_at__isnull=True).distinct()
    push = Person.objects.none()
    if push_apps:
        push = subscribed("push").filter(push_memberships__push_app__in=list(push_apps)).distinct()
    per_channel = {"email": email, "telegram": telegram, "push": push}
    ids = set()
    for channel in channels:
        if channel in per_channel:
            ids.update(per_channel[channel].values_list("pk", flat=True))
    by_language = {}
    default = Language.default_code()
    for code in Person.objects.filter(pk__in=ids).values_list("language__code", flat=True):
        by_language[code or default] = by_language.get(code or default, 0) + 1
    without = 0
    if channels:
        without = (
            Subscription.objects.filter(list__in=lists, channel__in=channels, state="on", consent__isnull=True)
            .values("person_id").distinct().count()
        )
    return {
        "people": len(ids),
        "email": email.count(),
        "telegram": telegram.count(),
        "push": push.count(),
        "by_language": by_language,
        "without_consent": without,
    }
