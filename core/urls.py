from django.urls import path

from . import dashboard, settings_views, views

urlpatterns = [
    path("", dashboard.dashboard, name="dashboard"),
    path("sign-in/", views.sign_in, name="sign_in"),
    path("sign-out/", views.sign_out, name="sign_out"),
    path("invite/<str:token>/", views.accept_invite, name="accept_invite"),
    path("setup/", views.setup, name="setup"),
    path("setup/<int:step>/", views.setup, name="setup_step"),
    path("search/", views.search, name="search"),
] + settings_views.urlpatterns
