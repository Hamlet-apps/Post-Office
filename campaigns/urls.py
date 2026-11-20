from django.urls import path

from . import views

app_name = "campaigns"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("new/", views.new, name="new"),
    path("<str:public_id>/", views.report, name="report"),
    path("<str:public_id>/design/", views.design, name="design"),
    path("<str:public_id>/send/", views.send, name="send"),
    path("<str:public_id>/audience/", views.audience, name="audience"),
    path("<str:public_id>/schedule/", views.schedule, name="schedule"),
    path("<str:public_id>/test/", views.test, name="test"),
    path("<str:public_id>/pause/", views.pause, name="pause"),
    path("<str:public_id>/resume/", views.resume, name="resume"),
    path("<str:public_id>/cancel/", views.cancel, name="cancel"),
    path("<str:public_id>/duplicate/", views.duplicate, name="duplicate"),
    path("<str:public_id>/delete/", views.delete, name="delete"),
    path("<str:public_id>/rename/", views.rename, name="rename"),
    path("<str:public_id>/save-template/", views.save_template, name="save_template"),
    path("<str:public_id>/export/", views.export, name="export"),
]
