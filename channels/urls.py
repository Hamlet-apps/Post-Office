from django.urls import path

from . import views

app_name = "channels"

urlpatterns = [
    path("", views.index, name="index"),
    path("email/add/", views.sender_add, name="sender_add"),
    path("email/<str:public_id>/edit/", views.sender_edit, name="sender_edit"),
    path("email/<str:public_id>/test/", views.sender_test, name="sender_test"),
    path("email/<str:public_id>/default/", views.sender_default, name="sender_default"),
    path("email/<str:public_id>/delete/", views.sender_delete, name="sender_delete"),
    path("telegram/add/", views.bot_add, name="bot_add"),
    path("telegram/<str:public_id>/edit/", views.bot_edit, name="bot_edit"),
    path("telegram/<str:public_id>/default/", views.bot_default, name="bot_default"),
    path("telegram/<str:public_id>/delete/", views.bot_delete, name="bot_delete"),
    path("push/connect/", views.push_connect, name="push_connect"),
    path("push/<str:public_id>/edit/", views.push_edit, name="push_edit"),
    path("push/<str:public_id>/test/", views.push_test, name="push_test"),
    path("push/<str:public_id>/secret/", views.push_secret, name="push_secret"),
    path("push/<str:public_id>/delete/", views.push_delete, name="push_delete"),
    path("webhooks/add/", views.webhook_add, name="webhook_add"),
    path("webhooks/<str:public_id>/toggle/", views.webhook_toggle, name="webhook_toggle"),
    path("webhooks/<str:public_id>/test/", views.webhook_test, name="webhook_test"),
    path("webhooks/<str:public_id>/secret/", views.webhook_secret, name="webhook_secret"),
    path("webhooks/<str:public_id>/delete/", views.webhook_delete, name="webhook_delete"),
]
