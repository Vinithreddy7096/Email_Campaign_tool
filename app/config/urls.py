"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from common import health
from common.metrics import metrics_view
from suppression.views import unsubscribe_view

urlpatterns = [
    path("admin/", admin.site.urls),
    # §9: liveness checks the process only; readiness checks critical deps.
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    path("metrics", metrics_view, name="metrics"),  # Bearer METRICS_TOKEN
    path("api/v1/", include("api.urls")),
    path("account/", include("accounts.urls")),
    # Public, no login (BUILD.md §10.8.4). Short path: it appears in every email.
    path("u/<str:token>", unsubscribe_view, name="unsubscribe"),
    # The operations console is the main UI (docs/design/console-mockup.html).
    # Last, so it never shadows admin, the API or the probes.
    path("", include("console.urls")),
]
