"""Root URL configuration."""

from django.contrib import admin
from django.urls import include, path

from common import health

urlpatterns = [
    path("admin/", admin.site.urls),
    # §9: liveness checks the process only; readiness checks critical deps.
    path("health/live", health.live, name="health-live"),
    path("health/ready", health.ready, name="health-ready"),
    # Catch-all last so it never shadows admin or the probes.
    path("", include("ui.urls")),
]
