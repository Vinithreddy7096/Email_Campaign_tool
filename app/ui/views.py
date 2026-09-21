"""Operator-facing views."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from config.celery import QUEUE_NAMES


def home(request: HttpRequest) -> HttpResponse:
    """Platform status.

    Deliberately states what the system cannot yet do. An operator landing here
    should never be left guessing whether this instance can send.
    """
    return render(
        request,
        "ui/home.html",
        {
            "queues": QUEUE_NAMES,
            "mta_target": f"{settings.MTA_SMTP_HOST}:{settings.MTA_SMTP_PORT}",
            "allowed_test_domains": settings.ALLOWED_TEST_RECIPIENT_DOMAINS,
        },
    )
