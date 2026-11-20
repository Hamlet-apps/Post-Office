"""The rules about people, subscriptions and consent, in one place (spec section 8).

Every screen and API endpoint goes through these functions rather than writing
Subscription or ConsentRecord rows directly.
"""
from django.db import transaction
from django.utils import timezone

from core.models import Language, Organization

from .models import (CHANNELS, ConsentRecord, List, Person, PersonExternalId, Subscription, Suppression, hash_email,
                     normalize_email)


class ConsentRequired(Exception):
    """A newsletter subscription was about to be switched on without consent."""


def find_person(email=None, building=None, external_id=None):
    """Match by the building's external id first, then by email."""
    if building is not None and external_id:
        link = PersonExternalId.objects.filter(building=building, external_id=str(external_id)).select_related("person").first()
        if link:
            return link.person
    email = normalize_email(email)
    if email:
        return Person.objects.filter(email=email).first()
    return None


@transaction.atomic
def find_or_create_person(email=None, first_name="", last_name="", language_code=None, source="",
                          building=None, external_id=None, attributes=None, update=True):
    """Return (person, created).

    An existing person keeps their language (language_code is used only when
    they have none); names and attributes are filled in or updated when update is true.
    """
    person = find_person(email, building, external_id)
    created = person is None
    language = Language.objects.filter(code=language_code).first() if language_code else None
    if created:
        person = Person(email=normalize_email(email), first_name=first_name or "", last_name=last_name or "",
                        language=language, attributes=attributes or {}, source=source)
        person.save()
    elif update:
        changed = False
        if first_name and first_name != person.first_name:
            person.first_name, changed = first_name, True
        if last_name and last_name != person.last_name:
            person.last_name, changed = last_name, True
        if normalize_email(email) and not person.email:
            person.email, changed = normalize_email(email), True
        if language and person.language_id is None:
            person.language, changed = language, True
        if attributes:
            person.attributes = {**(person.attributes or {}), **attributes}
            changed = True
        if changed:
            person.save()
    if building is not None and external_id:
        PersonExternalId.objects.get_or_create(building=building, external_id=str(external_id), defaults={"person": person})
    return person, created


def record_consent(person, lists, method, wording, language="", ip=None, user_agent="", link_params=None,
                   note="", recorded_by=None, given_at=None, confirmed=False):
    """Write the proof of consent. Staff consent needs a note saying how it was given."""
    if method == "staff" and not (note or "").strip():
        raise ConsentRequired("Say how this person gave consent.")
    now = timezone.now()
    consent = ConsentRecord.objects.create(
        person=person, method=method, wording=wording or "", language=language or person.language_code,
        ip=ip or None, user_agent=(user_agent or "")[:1000], link_params=link_params or {}, note=note or "",
        recorded_by=recorded_by, given_at=given_at or now, confirmed_at=now if confirmed else None,
    )
    consent.lists.set(lists)
    return consent


def set_subscription(person, lst, channel, on, consent=None, unconfirmed=False):
    """Switch one person/list/channel on or off.

    Turning a campaign-list (newsletter) subscription on needs a ConsentRecord,
    unless the person already has that subscription on. App lists need none.
    """
    assert channel in CHANNELS, channel
    sub = Subscription.objects.filter(person=person, list=lst, channel=channel).first()
    if not on:
        if sub and sub.state != "off":
            sub.state = "off"
            sub.save(update_fields=["state", "updated_at"])
        elif sub is None:
            sub = Subscription.objects.create(person=person, list=lst, channel=channel, state="off")
        return sub
    if sub and sub.state == "on":
        return sub
    if lst.kind == "campaign" and consent is None and not (sub and sub.consent_id):
        raise ConsentRequired(f"{lst.display_name()} is a newsletter list; switching it on needs recorded consent.")
    state = "unconfirmed" if unconfirmed else "on"
    if sub is None:
        return Subscription.objects.create(person=person, list=lst, channel=channel, state=state, consent=consent)
    sub.state = state
    if consent is not None:
        sub.consent = consent
    sub.save(update_fields=["state", "consent", "updated_at"])
    return sub


def confirm_consent(consent):
    """The person pressed the confirm button in the confirmation email."""
    if consent.confirmed_at is None:
        consent.confirmed_at = timezone.now()
        consent.save(update_fields=["confirmed_at", "updated_at"])
    Subscription.objects.filter(consent=consent, state="unconfirmed").update(state="on", updated_at=timezone.now())


def needs_email_confirmation():
    return Organization.get().confirm_by_email


def unsubscribe_all_news(person, channel=None):
    """Set every campaign-list subscription to off (optionally for one channel only)."""
    qs = Subscription.objects.filter(person=person, list__kind="campaign").exclude(state="off")
    if channel:
        qs = qs.filter(channel=channel)
    return qs.update(state="off", updated_at=timezone.now())


def reachable_channels(person, bot=None):
    """Channels on which this person can be reached at all, whatever their subscriptions say."""
    out = []
    if person.email_reachable:
        out.append("email")
    links = person.telegram_links.filter(blocked_at__isnull=True)
    if bot is not None:
        links = links.filter(bot=bot)
    if links.exists():
        out.append("telegram")
    if person.push_memberships.exists():
        out.append("push")
    return out


def subscription_grid(person):
    """The preferences grid: lists by channels.

    Returns {"campaign": [row, ...], "app": [row, ...]} where row is
    {"list": List, "cells": {"email": state_or_None, "telegram": ..., "push": ...}}.
    """
    subs = {}
    for s in person.subscriptions.all():
        subs[(s.list_id, s.channel)] = s.state
    grid = {"campaign": [], "app": []}
    for lst in List.objects.all():
        cells = {ch: subs.get((lst.pk, ch)) for ch in CHANNELS}
        if lst.kind == "app" and not any(cells.values()):
            continue  # app lists appear only for people a building added
        grid[lst.kind].append({"list": lst, "cells": cells})
    return grid


@transaction.atomic
def delete_person(person):
    """Remove their data but keep a hashed email so an import cannot silently resubscribe them."""
    if person.email:
        Suppression.objects.get_or_create(email_hash=hash_email(person.email))
    person.delete()


# --------------------------------------------------------------------------- helpers for the screens

def channel_columns():
    """The channel columns worth showing: email always, Telegram when a bot exists, push when a push app exists."""
    from channels.models import PushApp, TelegramBot

    columns = ["email"]
    if TelegramBot.objects.exists():
        columns.append("telegram")
    if PushApp.objects.exists():
        columns.append("push")
    return columns


def display_grid(person, columns=None):
    """subscription_grid shaped for a page.

    Returns {"columns": [...], "campaign": [row], "app": [row]}; each row is
    {"list": List, "cells": [{"channel", "state", "on", "unconfirmed", "available"}]}.
    A cell that is not available is drawn as a dash: no email address, or push for a
    list no push app serves (push for app lists is controlled inside each app).
    """
    from channels.models import PushApp

    columns = columns or channel_columns()
    push_lists = set()
    if "push" in columns:
        push_lists = set(PushApp.objects.filter(members__person=person, list__isnull=False).values_list("list_id", flat=True))
    grid = subscription_grid(person)
    out = {"columns": columns, "campaign": [], "app": []}
    for kind in ("campaign", "app"):
        for row in grid[kind]:
            lst = row["list"]
            cells = []
            for channel in columns:
                state = row["cells"].get(channel)
                if channel == "email":
                    available = bool(person.email)
                elif channel == "telegram":
                    available = True
                else:
                    available = kind == "campaign" and lst.pk in push_lists
                cells.append({"channel": channel, "state": state, "on": state == "on",
                              "unconfirmed": state == "unconfirmed", "available": available})
            out[kind].append({"list": lst, "cells": cells})
    return out
