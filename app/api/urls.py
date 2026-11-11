from django.urls import path

from api import campaigns, events, messages, views

app_name = "api"

urlpatterns = [
    path("whoami", views.whoami, name="whoami"),
    # BUILD.md §10.6.5
    path("campaigns", campaigns.create, name="campaign-create"),
    path("campaigns/<uuid:pk>", campaigns.detail, name="campaign-detail"),
    path("campaigns/<uuid:pk>/submit", campaigns.submit, name="campaign-submit"),
    path("campaigns/<uuid:pk>/approve", campaigns.approve, name="campaign-approve"),
    path("campaigns/<uuid:pk>/schedule", campaigns.schedule, name="campaign-schedule"),
    path("campaigns/<uuid:pk>/pause", campaigns.pause, name="campaign-pause"),
    path("campaigns/<uuid:pk>/metrics", campaigns.metrics, name="campaign-metrics"),
    # BUILD.md §10.6.6 — transactional sends
    path("messages", messages.send, name="message-send"),
    path("messages/<uuid:pk>", messages.detail, name="message-detail"),
    # BUILD.md §10.8.1 — signed, no token
    path("events/provider", events.provider_webhook, name="events-provider"),
    path("events/mail", events.mta_mail, name="events-mail"),
    path("events/postfix-log", events.mta_log, name="events-postfix-log"),
]
