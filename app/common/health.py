"""
Health endpoints (BUILD.md §9).

``/health/live``  — process is up. Never touches a dependency, so a database
                    blip cannot cause an orchestrator to kill healthy pods.
``/health/ready`` — this instance can actually do work: database, cache, broker.
"""

from __future__ import annotations

from typing import Any

from django.db import connection
from django.http import HttpRequest, JsonResponse


def live(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def ready(request: HttpRequest) -> JsonResponse:
    checks: dict[str, Any] = {
        "database": _check_database(),
        "cache": _check_cache(),
        "broker": _check_broker(),
    }
    healthy = all(c["ok"] for c in checks.values())
    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status=200 if healthy else 503,
    )


def _check_database() -> dict[str, Any]:
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _check_cache() -> dict[str, Any]:
    from django.core.cache import cache

    try:
        cache.set("healthcheck", "1", timeout=5)
        if cache.get("healthcheck") != "1":
            return {"ok": False, "error": "cache read-back mismatch"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _check_broker() -> dict[str, Any]:
    from config.celery import app as celery_app

    try:
        conn = celery_app.connection()
        conn.ensure_connection(max_retries=0, timeout=3)
        conn.release()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}
