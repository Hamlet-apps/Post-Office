from django.urls import path

from . import views

app_name = "people"

urlpatterns = [
    path("", views.list_view, name="list"),
    path("new/", views.person_new, name="person_new"),
    path("import/", views.import_view, name="import"),
    path("signup-form/", views.signup_form, name="signup_form"),
    path("lists/new/", views.list_new, name="list_new"),
    path("lists/<str:public_id>/edit/", views.list_edit, name="list_edit"),
    path("lists/<str:public_id>/delete/", views.list_delete, name="list_delete"),
    path("<str:public_id>/", views.person_view, name="person"),
    path("<str:public_id>/subscription/", views.person_subscription, name="person_subscription"),
    path("<str:public_id>/email-working/", views.person_clear_bounce, name="person_clear_bounce"),
    path("<str:public_id>/connect-link/", views.person_connect_link, name="person_connect_link"),
    path("<str:public_id>/stop-news/", views.person_stop_news, name="person_stop_news"),
    path("<str:public_id>/delete/", views.person_delete, name="person_delete"),
]
