from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("health", views.health, name="health"),
    path("messages", views.messages, name="messages"),
    path("messages/<str:message_id>", views.message_detail, name="message_detail"),
    path("templates", views.templates, name="templates"),
    path("lists/<str:slug>", views.list_detail, name="list_detail"),
    path("lists/<str:slug>/members/<str:external_id>", views.list_member, name="list_member"),
    path("push-apps/<str:audience>/members/<str:external_id>", views.push_member, name="push_member"),
    path("subscribe", views.subscribe, name="subscribe"),
    path("people/<str:person_id>/preferences-link", views.preferences_link, name="preferences_link"),
    path("commands/<str:log_id>/reply", views.command_reply, name="command_reply"),
]
