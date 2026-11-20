"""Open pixel and click redirect, included at /t/. No sign-in."""
from django.urls import path

from . import views

urlpatterns = [
    path("o/<str:token>.gif", views.open_pixel, name="track_open"),
    path("c/<str:token>", views.click, name="track_click"),
]
