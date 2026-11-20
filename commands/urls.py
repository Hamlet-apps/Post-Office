from django.urls import path

from . import views

app_name = "commands"

urlpatterns = [
    path("", views.index, name="index"),
    path("save/", views.save, name="save"),
    path("texts/", views.texts, name="texts"),
    path("preview/", views.preview, name="preview"),
    path("test/", views.test, name="test"),
    path("<str:command_id>/toggle/", views.toggle, name="toggle"),
    path("<str:command_id>/delete/", views.delete, name="delete"),
]
