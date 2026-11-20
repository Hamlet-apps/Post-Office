from django.shortcuts import redirect
from django.conf import settings
from django.utils import timezone, translation

from .models import Organization, TeamMember

# Paths that must work before an owner exists.
_OPEN_PREFIXES = ("/setup/", "/static/", "/media/", "/api/", "/t/", "/tg/", "/join", "/p/", "/u/", "/confirm/")


class FirstRunMiddleware:
    """Until an owner exists, every staff page leads to the four-step setup.

    Also shows dates and times in the organization's time zone on every page.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._has_owner = False

    def __call__(self, request):
        if not self._has_owner:
            self._has_owner = TeamMember.objects.filter(role="owner").exists()
            if not self._has_owner and not request.path.startswith(_OPEN_PREFIXES):
                return redirect("/setup/")
        # Every request starts in English. Reader-facing pages, emails and bot replies switch to
        # the reader's language themselves; the staff screens stay English until their
        # Ukrainian and French catalogues are complete.
        translation.activate(settings.LANGUAGE_CODE)
        request.LANGUAGE_CODE = settings.LANGUAGE_CODE
        if not request.path.startswith(("/static/", "/media/", "/t/")):
            timezone.activate(Organization.get().tz)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
