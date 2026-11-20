from django.conf import settings
from django.urls import include, path, re_path
from django.views.static import serve

urlpatterns = [
    path("", include("core.urls")),  # dashboard, sign-in, setup, settings
    path("campaigns/", include("campaigns.urls")),
    path("templates/", include("content.urls")),
    path("people/", include("people.urls")),
    path("outbox/", include("delivery.urls")),
    path("channels/", include("channels.urls")),
    path("commands/", include("commands.urls")),
    path("api/v1/", include("api.urls")),
    path("t/", include("delivery.public_urls")),  # open pixel and click redirect
    path("tg/", include("channels.public_urls")),  # Telegram webhook
    path("", include("public.urls")),  # /join, /p/<token>, /u/<token>, /confirm/<token>
    # Uploaded images (local media volume). Small installs serve them from the app itself.
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
