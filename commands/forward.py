"""Passing a command on to a web address (spec section 12).

Post Office is a plain proxy: it builds the request the owner described, waits
up to 10 seconds and hands back whatever answer comes.

    values = build_values("shifts", "/shifts saturday", person=person, bot=bot, chat_id=48213377, log=log)
    result = send(command, values)        # ForwardResult(outcome, reply_text, ...)

TODO(switchboard): Switchboard is the spec's second target. When it exists, add
a `send_to_switchboard(command, values)` next to `send` (post `standard_payload`
to the workflow's trigger address) and choose between them on `Command.target`
in `forward(...)` below.

Header values are secrets: they are stored encrypted, decrypted only to build
the outgoing request, and never written to a log or an error message.
"""
import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import httpx
from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from core.fields import decrypt, encrypt, mask

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
NO_BODY_METHODS = ("GET", "DELETE")
TELEGRAM_TEXT_LIMIT = 4096

# (placeholder, label on the insert button, sample value for the preview)
PLACEHOLDERS = [
    ("command", gettext_lazy("command"), "feedback"),
    ("text", gettext_lazy("full text"), "/feedback more parking please"),
    ("arguments", gettext_lazy("arguments"), "more parking please"),
    ("person.first_name", gettext_lazy("first name"), "Taras"),
    ("person.language", gettext_lazy("language"), "uk"),
    ("person.id", gettext_lazy("person id"), "p_2041"),
    ("person.telegram_id", gettext_lazy("Telegram id"), "48213377"),
    ("bot", gettext_lazy("bot"), "MapleGroveBot"),
]
# Two extra placeholders the suggested default body needs: the arguments as a
# JSON list (inserted as JSON, not as a string) and the address for a later answer.
RAW_JSON_PLACEHOLDERS = ("arguments.list",)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_.]+)\s*\}\}")

# The standard payload: the suggested default body for a web address.
DEFAULT_BODY = """{
  "command": "{{command}}",
  "text": "{{text}}",
  "arguments": {{arguments.list}},
  "bot": "{{bot}}",
  "person": {"id": "{{person.id}}", "first_name": "{{person.first_name}}", "language": "{{person.language}}", "telegram_id": {{person.telegram_id}}},
  "reply_url": "{{reply_url}}"
}"""


class TargetRefused(Exception):
    """The address is not one Post Office will call. The message is safe to show to staff."""


@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: dict = field(default_factory=dict)  # decrypted values: never log or display
    body: str = ""

    def display(self, masked=True):
        """The request as text for the preview. Header values are masked unless told otherwise."""
        lines = [f"{self.method} {self.url}"]
        for name, value in self.headers.items():
            lines.append(f"{name}: {mask(value) if masked else value}")
        if self.body:
            lines += ["", self.body]
        return "\n".join(lines)


@dataclass
class ForwardResult:
    outcome: str  # answered, no_answer or error (CommandLog.result values)
    reply_text: str = ""  # what to send to the person; empty sends nothing
    status: int = 0
    detail: str = ""  # a plain-language reason for staff; never holds header values
    duration_ms: int = 0

    @property
    def ok(self):
        return self.outcome == "answered"


# --------------------------------------------------------------------------- values

def reply_url(log):
    if log is None:
        return f"{settings.PUBLIC_URL}/api/v1/commands/log_0/reply?token=test"
    return f"{settings.PUBLIC_URL}/api/v1/commands/{log.public_id}/reply?token={log.reply_token}"


def split_arguments(text):
    """'/name@Bot arg1 arg2' -> (command, arguments string, arguments list)."""
    text = (text or "").strip()
    head, _sep, rest = text.partition(" ")
    if "\n" in head:  # "/name\nmore" counts the rest as arguments too
        head, _nl, tail = head.partition("\n")
        rest = (tail + " " + rest).strip()
    name = head[1:] if head.startswith("/") else head
    name = name.split("@", 1)[0].lower()
    rest = rest.strip()
    return name, rest, rest.split()


def build_values(command, text, *, person=None, bot=None, chat_id=None, log=None, first_name="", language=""):
    """The placeholder values for one incoming message.

    `person` is a people.Person or None (an unknown chat). `first_name` and
    `language` fill in for an unknown person (the test button uses them).
    """
    _name, arguments, argument_list = split_arguments(text)
    return {
        "command": command,
        "text": text,
        "arguments": arguments,
        "arguments.list": argument_list,
        "person.id": person.public_id if person is not None else "",
        "person.first_name": (person.first_name if person is not None else first_name) or "",
        "person.language": (person.language_code if person is not None else language) or "",
        "person.telegram_id": chat_id if chat_id is not None else "",
        "bot": (bot.username or bot.name) if bot is not None else "",
        "reply_url": reply_url(log),
    }


def sample_values(command_name="feedback", bot=None):
    """Values for the live preview: the mockup's sample person, Taras."""
    name = command_name or "feedback"
    values = build_values(name, f"/{name} more parking please", bot=bot, chat_id=48213377,
                          first_name="Taras", language="uk")
    values["person.id"] = "p_2041"
    if bot is None:
        values["bot"] = "MapleGroveBot"
    return values


def standard_payload(values):
    """The standard payload as a dict (spec section 12). TODO(switchboard): posted as is to Switchboard."""
    telegram_id = values.get("person.telegram_id")
    return {
        "command": values.get("command", ""),
        "text": values.get("text", ""),
        "arguments": values.get("arguments.list", []),
        "bot": values.get("bot", ""),
        "person": {
            "id": values.get("person.id", ""),
            "first_name": values.get("person.first_name", ""),
            "language": values.get("person.language", ""),
            "telegram_id": telegram_id if telegram_id != "" else None,
        },
        "reply_url": values.get("reply_url", ""),
    }


# --------------------------------------------------------------------------- filling in

def _as_text(value):
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return "" if value is None else str(value)


def fill(template, values, mode="raw"):
    """Replace {{name}} placeholders.

    mode "url":  each value is URL-encoded (for the address)
    mode "json": each value is JSON-escaped, to sit inside a JSON string (or as a bare number)
    mode "raw":  values go in as they are
    Unknown names are left alone so a typing mistake shows up in the preview.
    """
    def replace(match):
        name = match.group(1)
        if name not in values:
            return match.group(0)
        value = values[name]
        if mode == "url":
            return quote(_as_text(value), safe="")
        if mode == "json":
            if name in RAW_JSON_PLACEHOLDERS:
                return json.dumps(value, ensure_ascii=False)
            return json.dumps(_as_text(value), ensure_ascii=False)[1:-1]
        return _as_text(value)

    return _PLACEHOLDER_RE.sub(replace, template or "")


def looks_like_json(body, headers=None):
    for name, value in (headers or {}).items():
        if name.lower() == "content-type":
            return "json" in (value or "").lower()
    return (body or "").lstrip()[:1] in ("{", "[")


# --------------------------------------------------------------------------- headers

def plain_headers(stored):
    """Stored header rows -> {name: decrypted value}. For building the request only."""
    out = {}
    for row in stored or []:
        name = (row.get("name") or "").strip()
        if name:
            out[name] = decrypt(row.get("value") or "")
    return out


def store_headers(rows, existing=None):
    """Form rows -> header rows ready to save, with every value encrypted.

    rows: [{"name": ..., "value": ..., "orig": index into `existing` or None}].
    A saved header is shown masked; a row that comes back with an empty or
    still-masked value keeps the value it had.
    """
    existing = existing or []
    out = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name or not _HEADER_NAME_RE.match(name):
            continue
        value = row.get("value") or ""
        orig = row.get("orig")
        if value in ("", mask("x")) and isinstance(orig, int) and 0 <= orig < len(existing):
            stored = existing[orig].get("value") or ""
            if stored and not stored.startswith("fernet:"):
                stored = encrypt(stored)
        else:
            stored = encrypt(value.replace("\r", "").replace("\n", ""))
        out.append({"name": name, "value": stored})
    return out


_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


# --------------------------------------------------------------------------- the request

def build_request(command, values):
    """The exact request for these values. `command` is a Command (saved or not)."""
    method = (command.method or "POST").upper()
    headers = plain_headers(command.headers)
    body = ""
    if method not in NO_BODY_METHODS and command.body:
        body = fill(command.body, values, "json" if looks_like_json(command.body, headers) else "raw")
    return PreparedRequest(method=method, url=fill((command.url or "").strip(), values, "url"), headers=headers, body=body)


def check_address(url):
    """Refuse addresses Post Office must not call. Raises TargetRefused.

    Loopback and link-local addresses are refused unless ALLOW_PRIVATE_TARGETS
    is set: the host is resolved and every address it has is checked.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise TargetRefused(_("This is not a web address."))
    if parts.scheme not in ("http", "https") or not host:
        raise TargetRefused(_("The address must start with https:// or http://."))
    if getattr(settings, "ALLOW_PRIVATE_TARGETS", False):
        return
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise TargetRefused(_("This address could not be found. Check the spelling."))
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            raise TargetRefused(_(
                "This address points at this server or its local network link, so Post Office will not call it. "
                "If that is what you want, set ALLOW_PRIVATE_TARGETS=1."
            ))


def extract_reply(response_text, reply_path):
    """Pick the reply out of the answer. Returns (text, problem)."""
    path = (reply_path or "").strip()
    if not path:
        return response_text or "", ""
    if not (response_text or "").strip():
        return "", ""
    try:
        node = json.loads(response_text)
    except ValueError:
        return "", _("The answer was not JSON, so the reply field could not be read.")
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return "", ""  # no such field: nothing to say
    if node is None or node is False:
        return "", ""
    if isinstance(node, (dict, list)):
        return json.dumps(node, ensure_ascii=False), ""
    return str(node), ""


def send(command, values, timeout=TIMEOUT_SECONDS):
    """Call the command's web address and read the answer. Never raises."""
    started = time.monotonic()

    def done(outcome, **kwargs):
        return ForwardResult(outcome=outcome, duration_ms=int((time.monotonic() - started) * 1000), **kwargs)

    request = build_request(command, values)
    try:
        check_address(request.url)
    except TargetRefused as exc:
        return done("error", detail=str(exc))
    try:
        response = httpx.request(
            request.method, request.url, headers=request.headers,
            content=request.body.encode("utf-8") if request.body else None,
            timeout=timeout, follow_redirects=False,
        )
    except httpx.TimeoutException:
        return done("no_answer", detail=_("No answer within %(seconds)s seconds.") % {"seconds": timeout})
    except httpx.HTTPError as exc:
        # Only the kind of failure: the exception text can repeat request details.
        logger.info("Command /%s: could not reach its address (%s)", command.name, exc.__class__.__name__)
        return done("error", detail=_("Could not connect to the address."))
    except Exception as exc:  # a malformed address or header
        logger.info("Command /%s: the request could not be sent (%s)", command.name, exc.__class__.__name__)
        return done("error", detail=_("The request could not be sent. Check the address and the headers."))
    status = response.status_code
    if not 200 <= status < 300:
        return done("error", status=status, detail=_("The address answered with an error (status %(status)s).") % {"status": status})
    text, problem = extract_reply(response.text, command.reply_path)
    if problem:
        return done("error", status=status, detail=problem)
    text = text.strip()
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = text[:TELEGRAM_TEXT_LIMIT - 1].rstrip() + "…"
    return done("answered", status=status, reply_text=text)


def forward(command, values, timeout=TIMEOUT_SECONDS):
    """Pass a command to its target.

    TODO(switchboard): when Command.target gains "switchboard", branch here.
    """
    return send(command, values, timeout=timeout)
