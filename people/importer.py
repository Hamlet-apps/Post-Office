"""Importing people from a CSV file (spec sections 8 and 11).

    parsed = parse_csv(uploaded_bytes)          # header and rows
    plan = plan_import(parsed, target_list)     # what would happen: new, updated, skipped
    result = run_import(parsed, target_list, consent_given=True, by=request.user)

run_import refuses (ConsentRequired) unless the importer ticked "these people agreed
to receive this". It never resubscribes a deleted (suppressed) address or someone who
unsubscribed from the target list.
"""
import csv
import io
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils.translation import gettext as _

from core.models import Language

from . import services
from .models import Person, Subscription, Suppression, hash_email, normalize_email
from .services import ConsentRequired

MAX_BYTES = 5 * 1024 * 1024
KNOWN = ("email", "first_name", "last_name", "language")
ALIASES = {
    "e-mail": "email", "e_mail": "email", "email_address": "email", "mail": "email",
    "firstname": "first_name", "first": "first_name", "given_name": "first_name",
    "lastname": "last_name", "last": "last_name", "surname": "last_name", "family_name": "last_name",
    "lang": "language", "locale": "language",
}

# Why a row is skipped.
SKIP_INVALID = "invalid"
SKIP_REPEATED = "repeated"
SKIP_SUPPRESSED = "suppressed"
SKIP_UNSUBSCRIBED = "unsubscribed"


class ImportError_(ValueError):
    """The file cannot be read as a list of people. The message is shown to staff."""


@dataclass
class ParsedImport:
    columns: list  # the file's own header names, in order
    extra_columns: list  # those that become extra details
    rows: list  # [{"email", "first_name", "last_name", "language", "attributes": {...}, "line": 2}]


@dataclass
class ImportPlan:
    new: int = 0
    updated: int = 0
    skipped: int = 0
    reasons: dict = field(default_factory=dict)  # reason -> count
    actions: list = field(default_factory=list)  # one of "new", "updated" or a skip reason, per row

    @property
    def total(self):
        return self.new + self.updated + self.skipped

    @property
    def suppressed(self):
        return self.reasons.get(SKIP_SUPPRESSED, 0)


def skip_reason_label(reason):
    return {
        SKIP_INVALID: _("Not an email address"),
        SKIP_REPEATED: _("Repeated in the file"),
        SKIP_SUPPRESSED: _("Deleted before, so not added again"),
        SKIP_UNSUBSCRIBED: _("Unsubscribed from this list"),
    }.get(reason, reason)


def action_label(action):
    if action == "new":
        return _("New")
    if action == "updated":
        return _("Updated")
    return skip_reason_label(action)


def _decode(data):
    if isinstance(data, str):
        return data.lstrip("﻿")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_(_("This file could not be read. Save it as CSV (UTF-8) and try again."))


def _key(header):
    key = (header or "").strip().lower().replace(" ", "_")
    return ALIASES.get(key, key)


def parse_csv(data):
    """Read CSV bytes or text. The first row is the header and must have an email column."""
    if not isinstance(data, str) and len(data) > MAX_BYTES:
        raise ImportError_(_("This file is larger than 5 MB. Split it into smaller files."))
    text = _decode(data)
    if not text.strip():
        raise ImportError_(_("This file is empty."))
    first_line = text.splitlines()[0]
    delimiter = max(",;\t", key=first_line.count) if any(c in first_line for c in ",;\t") else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise ImportError_(_("This file is empty."))
    columns = [h.strip() for h in header]
    keys = [_key(h) for h in columns]
    if "email" not in keys:
        raise ImportError_(_("The first row must name the columns, and one of them must be \"email\"."))
    extra = [c for c, k in zip(columns, keys) if c and k not in KNOWN]
    rows = []
    try:
        for line, values in enumerate(reader, start=2):
            if not any((v or "").strip() for v in values):
                continue
            row = {"email": "", "first_name": "", "last_name": "", "language": "", "attributes": {}, "line": line}
            for column, key, value in zip(columns, keys, values):
                value = (value or "").strip()
                if key in KNOWN:
                    row[key] = value
                elif column and value:
                    row["attributes"][column] = value
            rows.append(row)
    except csv.Error:
        raise ImportError_(_("This file could not be read as CSV."))
    if not rows:
        raise ImportError_(_("This file has a header but no people in it."))
    return ParsedImport(columns=columns, extra_columns=extra, rows=rows)


def _language_lookup():
    lookup = {}
    for lang in Language.objects.all():
        for name in (lang.code, lang.name, lang.english_name):
            if name:
                lookup[name.strip().lower()] = lang.code
    return lookup


def language_code_for(value, lookup=None):
    """Match a language column value ("uk", "Ukrainian", "Українська") to a code, or None for the default."""
    lookup = _language_lookup() if lookup is None else lookup
    value = (value or "").strip().lower()
    return lookup.get(value) or lookup.get(value.split("-")[0])


def plan_import(parsed, lst):
    """Work out what each row would do, without changing anything."""
    emails = {normalize_email(r["email"]) for r in parsed.rows if r["email"]}
    emails.discard(None)
    existing = set()
    unsubscribed = set()
    suppressed = set()
    batch = list(emails)
    for start in range(0, len(batch), 500):
        chunk = batch[start:start + 500]
        existing.update(Person.objects.filter(email__in=chunk).values_list("email", flat=True))
        unsubscribed.update(Subscription.objects.filter(
            person__email__in=chunk, list=lst, channel="email", state="off").values_list("person__email", flat=True))
        hashes = {hash_email(e): e for e in chunk}
        for h in Suppression.objects.filter(email_hash__in=list(hashes)).values_list("email_hash", flat=True):
            suppressed.add(hashes[h])

    plan = ImportPlan()
    seen = set()
    for row in parsed.rows:
        email = normalize_email(row["email"])
        action = None
        try:
            validate_email(email or "")
        except ValidationError:
            action = SKIP_INVALID
        if action is None and email in seen:
            action = SKIP_REPEATED
        if action is None and email in suppressed and email not in existing:
            action = SKIP_SUPPRESSED
        if action is None and email in unsubscribed:
            action = SKIP_UNSUBSCRIBED
        if action is None:
            action = "updated" if email in existing else "new"
        if email:
            seen.add(email)
        plan.actions.append(action)
        if action == "new":
            plan.new += 1
        elif action == "updated":
            plan.updated += 1
        else:
            plan.skipped += 1
            plan.reasons[action] = plan.reasons.get(action, 0) + 1
    return plan


def import_wording():
    """The sentence beside the required tick box; stored as the consent wording."""
    return _("These people agreed to receive this.")


@transaction.atomic
def run_import(parsed, lst, *, consent_given, by=None, file_name=""):
    """Add or update the people and subscribe them to the campaign list by email.

    Each person gets a ConsentRecord with method "import". Returns the ImportPlan that was carried out.
    """
    if not consent_given:
        raise ConsentRequired(_("Tick the box to say these people agreed to receive this. Without it nothing is imported."))
    if lst is None or lst.kind != "campaign":
        raise ImportError_(_("Choose a campaign list to add these people to."))
    plan = plan_import(parsed, lst)
    lookup = _language_lookup()
    wording = import_wording()
    note = _("Imported from %(file)s") % {"file": file_name} if file_name else ""
    for row, action in zip(parsed.rows, plan.actions):
        if action not in ("new", "updated"):
            continue
        person, _created = services.find_or_create_person(
            email=row["email"], first_name=row["first_name"][:120], last_name=row["last_name"][:120],
            language_code=language_code_for(row["language"], lookup), source="import",
            attributes=row["attributes"] or None)
        consent = services.record_consent(person, [lst], "import", wording, language=person.language_code,
                                          note=note, recorded_by=by)
        services.set_subscription(person, lst, "email", True, consent=consent)
    return plan
