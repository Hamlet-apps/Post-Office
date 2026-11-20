"""Roles (spec section 13).

viewer: see dashboard, reports, outbox.
editor: also write and send campaigns, edit templates, manage people.
owner:  also channels, commands, settings, team, API keys.

Use on every staff view:

    @role_required("editor")
    def my_view(request): ...
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def role_required(role="viewer"):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not user.has_role(role):
                raise PermissionDenied("Your role cannot open this page.")
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


viewer_required = role_required("viewer")
editor_required = role_required("editor")
owner_required = role_required("owner")
