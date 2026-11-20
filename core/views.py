import zoneinfo

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .models import KNOWN_LANGUAGES, Building, Language, Organization, TeamMember
from .permissions import viewer_required

COMMON_TIMEZONES = [
    "America/Vancouver", "America/Edmonton", "America/Regina", "America/Winnipeg", "America/Toronto",
    "America/Halifax", "America/St_Johns", "America/Los_Angeles", "America/Denver", "America/Chicago",
    "America/New_York", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Warsaw", "Europe/Kyiv",
    "Asia/Kolkata", "Asia/Manila", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney", "UTC",
]


# --------------------------------------------------------------------------- sign-in

def sign_in(request):
    """Email and password sign-in.

    TODO(gatehouse): add "Continue with Gatehouse" (OpenID Connect) here once
    Gatehouse exists. The spec (section 13): show the button when the
    GATEHOUSE_* variables are set; match the Gatehouse user to a TeamMember by
    email, store the subject in TeamMember.gatehouse_subject, and refuse users
    who were not invited. The design mockup (design/mockups/SignIn.dc.html)
    places that button above the password form with an "or use a password"
    divider.
    """
    if request.user.is_authenticated:
        return redirect("dashboard")
    error = ""
    email = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        user = authenticate(request, username=email, password=request.POST.get("password", ""))
        if user is not None and user.is_active:
            login(request, user)
            nxt = request.POST.get("next") or request.GET.get("next") or ""
            if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
                return redirect(nxt)
            return redirect("dashboard")
        error = _("That email and password do not match. Please try again.")
    return render(request, "core/sign_in.html", {
        "error": error, "email": email, "next": request.GET.get("next", ""), "org": Organization.get(),
    })


@require_POST
def sign_out(request):
    logout(request)
    return redirect("sign_in")


def accept_invite(request, token):
    """An invited team member chooses their name and password."""
    member = TeamMember.objects.filter(invite_token=token, is_active=True).exclude(invite_token="").first()
    if member is None:
        return render(request, "core/invite.html", {"invalid": True, "org": Organization.get()})
    error = ""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        password = request.POST.get("password", "")
        try:
            validate_password(password)
        except ValidationError as exc:
            error = " ".join(exc.messages)
        if not error:
            member.name = name or member.name
            member.set_password(password)
            member.invite_token = ""
            member.save()
            login(request, member)
            return redirect("dashboard")
    return render(request, "core/invite.html", {"member": member, "error": error, "org": Organization.get()})


@viewer_required
def search(request):
    """The header search box: looks in people; campaigns are matched by the people screen's sibling link."""
    q = request.GET.get("q", "").strip()
    from campaigns.models import Campaign

    campaign = Campaign.objects.filter(name__icontains=q).first() if q else None
    if campaign and not request.GET.get("people"):
        from people.models import Person
        from django.db.models import Q

        has_people = Person.objects.filter(Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)).exists()
        if not has_people:
            return redirect(f"/campaigns/?q={q}")
    return redirect(f"/people/?q={q}")


# --------------------------------------------------------------------------- first run

SETUP_STEPS = [
    (1, "Your organisation"),
    (2, "Languages"),
    (3, "Channels"),
    (4, "Connect your buildings"),
]


def setup(request, step=None):
    """First run: four short steps. Steps 3 and 4 may be skipped."""
    org = Organization.get()
    has_owner = TeamMember.objects.filter(role="owner").exists()
    if org.setup_complete and has_owner:
        return redirect("dashboard")
    if not has_owner:
        step = 1
    else:
        if not request.user.is_authenticated:
            return redirect("sign_in")
        if not request.user.is_owner:
            return redirect("dashboard")
        if step is None or step < 2:
            step = 2 if not Language.objects.exists() else 3
        if step > 2 and not Language.objects.exists():
            step = 2
    handler = {1: _setup_org, 2: _setup_languages, 3: _setup_channels, 4: _setup_buildings}.get(step)
    if handler is None:
        return redirect("setup")
    return handler(request, org)


def _render_step(request, step, org, **ctx):
    ctx.update({"step": step, "steps": SETUP_STEPS, "org": org, "total": len(SETUP_STEPS)})
    return render(request, f"core/setup_{step}.html", ctx)


def _setup_org(request, org):
    data = {"timezone": org.timezone}
    errors = {}
    if request.method == "POST":
        data = {k: request.POST.get(k, "").strip() for k in ("org_name", "name", "email", "timezone", "mailing_address")}
        password = request.POST.get("password", "")
        if not data["org_name"]:
            errors["org_name"] = _("Enter the name of your organisation.")
        if not data["name"]:
            errors["name"] = _("Enter your name.")
        try:
            validate_email(data["email"])
        except ValidationError:
            errors["email"] = _("Enter a working email address.")
        try:
            validate_password(password)
        except ValidationError as exc:
            errors["password"] = " ".join(exc.messages)
        try:
            zoneinfo.ZoneInfo(data["timezone"])
        except Exception:
            errors["timezone"] = _("Choose a time zone.")
        if not errors:
            org.name = data["org_name"]
            org.timezone = data["timezone"]
            org.mailing_address = data["mailing_address"]
            org.reply_to = data["email"]
            org.save()
            owner = TeamMember.objects.create_user(data["email"], password, name=data["name"], role="owner")
            login(request, owner)
            return redirect("setup_step", step=2)
    return _render_step(request, 1, org, data=data, errors=errors, timezones=COMMON_TIMEZONES)


def _setup_languages(request, org):
    existing = {l.code: l for l in Language.objects.all()}
    selected = list(existing) or ["en"]
    default = Language.default_code() if existing else "en"
    error = ""
    if request.method == "POST":
        selected = [c for c in request.POST.getlist("languages") if any(c == k[0] for k in KNOWN_LANGUAGES)]
        default = request.POST.get("default", "")
        if not selected:
            error = _("Pick at least one language.")
        else:
            if default not in selected:
                default = selected[0]
            save_languages(selected, default)
            return redirect("setup_step", step=3)
    tiles = [{"code": c, "name": n, "english": e, "on": c in selected} for c, n, e in KNOWN_LANGUAGES]
    return _render_step(request, 2, org, tiles=tiles, default=default, error=error)


def save_languages(codes, default):
    """Create the chosen languages, drop the others (their people move to the default)."""
    known = {c: (n, e) for c, n, e in KNOWN_LANGUAGES}
    for position, code in enumerate(codes):
        name, english = known.get(code, (code, code))
        Language.objects.update_or_create(
            code=code, defaults={"name": name, "english_name": english, "position": position, "is_default": code == default}
        )
    default_lang = Language.objects.get(code=default)
    for lang in Language.objects.exclude(code__in=codes):
        lang.people.update(language=default_lang)
        lang.delete()


def _setup_channels(request, org):
    from channels.email import test_connection
    from channels.models import EmailSender

    sender = EmailSender.app_sender()
    data = {"kind": "gmail", "from_name": org.name, "from_email": "", "host": "", "port": "587", "username": ""}
    error = ""
    if request.method == "POST":
        if "skip" in request.POST:
            return redirect("setup_step", step=4)
        data = {k: request.POST.get(k, "").strip() for k in data}
        password = request.POST.get("password", "")
        gmail = data["kind"] == "gmail"
        candidate = sender or EmailSender(purpose="app")
        candidate.kind = "gmail" if gmail else "smtp"
        candidate.from_name = data["from_name"] or org.name
        candidate.from_email = data["from_email"]
        candidate.host = "smtp.gmail.com" if gmail else data["host"]
        candidate.port = 587 if gmail else int(data["port"] or 587)
        candidate.tls_mode = "ssl" if candidate.port == 465 else "starttls"
        candidate.username = data["from_email"] if gmail else (data["username"] or data["from_email"])
        candidate.password = password
        if not candidate.from_email or not password:
            error = _("Enter the email address and its password.")
        else:
            ok, problem = test_connection(candidate)
            if ok:
                candidate.state = "ok"
                candidate.last_error = ""
                candidate.is_default = True
                candidate.save()
                return redirect("setup_step", step=4)
            error = _("The mail server did not accept this: %(problem)s") % {"problem": problem}
    return _render_step(request, 3, org, data=data, error=error, sender=sender)


def _setup_buildings(request, org):
    new_key = None
    new_building = None
    error = ""
    if request.method == "POST":
        if "finish" in request.POST:
            org.setup_complete = True
            org.save(update_fields=["setup_complete", "updated_at"])
            messages.success(request, _("Your post office is open."))
            return redirect("dashboard")
        name = request.POST.get("name", "").strip()
        kind = request.POST.get("kind", "other")
        if not name:
            error = _("Give the building a name.")
        else:
            new_building = Building.objects.create(
                name=name, kind=kind if kind in dict(Building.KIND_CHOICES) else "other",
                base_url=request.POST.get("base_url", "").strip(),
            )
            new_key = new_building.issue_key()
    return _render_step(
        request, 4, org, buildings=Building.objects.all(), kinds=Building.KIND_CHOICES,
        new_key=new_key, new_building=new_building, error=error, now=timezone.now(),
    )
