from django.urls import path

from . import views

app_name = "public"

urlpatterns = [
    path("join", views.join, name="join"),
    path("join/", views.join),
    path("confirm/<str:token>", views.confirm, name="confirm"),
    path("p/<str:token>", views.preferences, name="preferences"),
    path("p/<str:token>/toggle", views.preferences_toggle, name="preferences_toggle"),
    path("p/<str:token>/language", views.preferences_language, name="preferences_language"),
    path("p/<str:token>/unsubscribe-all", views.preferences_unsubscribe_all, name="preferences_unsubscribe_all"),
    path("u/<str:token>", views.unsubscribe, name="unsubscribe"),
]
