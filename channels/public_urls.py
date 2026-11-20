"""The Telegram webhook, included at /tg/. No sign-in; the secret in the address is the check."""
from django.urls import path

from . import views

urlpatterns = [
    path("<str:bot_id>/<str:secret>", views.telegram_webhook, name="telegram_webhook"),
]
