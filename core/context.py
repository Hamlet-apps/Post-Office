from .models import Language, Organization

# The side menu: (url name, label, icon, minimum role, path prefix that marks it current)
NAV = [
    ("dashboard", "Dashboard", "dashboard", "viewer", "/"),
    ("campaigns:list", "Campaigns", "campaigns", "viewer", "/campaigns/"),
    ("content:templates", "Templates", "templates", "viewer", "/templates/"),
    ("people:list", "People", "people", "viewer", "/people/"),
    ("delivery:outbox", "Outbox", "outbox", "viewer", "/outbox/"),
    ("channels:index", "Channels", "channels", "owner", "/channels/"),
    ("commands:index", "Commands", "commands", "owner", "/commands/"),
]


def shell(request):
    """Values every staff page needs: the organization, languages and the side menu."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    path = request.path
    items = []
    for name, label, icon, role, prefix in NAV:
        if not user.has_role(role):
            continue
        current = path == "/" if prefix == "/" else path.startswith(prefix)
        items.append({"url_name": name, "label": label, "icon": icon, "current": current})
    return {
        "org": Organization.get(),
        "org_languages": list(Language.objects.all()),
        "nav_items": items,
        "nav_settings_current": path.startswith("/settings/"),
    }
