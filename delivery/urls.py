from django.urls import path

from . import views

app_name = "delivery"

urlpatterns = [
    path("", views.outbox, name="outbox"),
    path("campaign/<str:public_id>/", views.campaign_messages, name="campaign_messages"),
    path("<str:public_id>/retry/", views.retry, name="retry"),
]
