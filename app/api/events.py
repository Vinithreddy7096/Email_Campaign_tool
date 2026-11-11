"""Signed event intake (BUILD.md §10.8.1, spec §9, §12).

POST /api/v1/events/provider     a mail provider's webhook (WEBHOOK_SIGNING_SECRET)
POST /api/v1/events/mail         one bounce/complaint email from our MTA (MTA_INGEST_SECRET)
POST /api/v1/events/postfix-log  Postfix delivery-status log lines from our MTA (MTA_INGEST_SECRET)

The MTA hosts run no application code: mta/bin/forward-bounce and
mta/bin/ship-postfix-log sign and post what Postfix produces. They use their own
secret so a leaked provider-webhook secret cannot inject bounces, and vice versa.

Authentication is the signature, not a token:

    X-Webhook-Timestamp: <unix seconds>
    X-Webhook-Signature: hex(HMAC-SHA256(WEBHOOK_SIGNING_SECRET, "<timestamp>.<raw body>"))

Body: {"id": "<provider event id>", "type": "delivered|deferred|bounce|hard_bounce|
soft_bounce|complaint|unsubscribe", "recipient": "...", "message_id": "<our Message-ID>",
"smtp_code": 550, "enhanced_code": "5.1.1", "occurred_at": "<ISO 8601>", "diagnostic": "..."}

Replay protection: a timestamp outside WEBHOOK_REPLAY_WINDOW_S is refused, and
an event id is processed once — a replayed id answers 200 "duplicate". The raw
body is stored before anything else; normalization runs asynchronously.
With no WEBHOOK_SIGNING_SECRET configured, every request is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from functools import partial

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from events.models import EventSource, RawPayload

MAX_LOG_LINES = 1000


def _signature_ok(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _refused(request: HttpRequest, secret: str) -> JsonResponse | None:
    """None if the request is signed with ``secret`` inside the replay window."""
    if not secret:
        return JsonResponse({"detail": "intake not configured"}, status=503)
    timestamp = request.headers.get("X-Webhook-Timestamp", "")
    signature = request.headers.get("X-Webhook-Signature", "")
    if not timestamp.isdigit() or not signature:
        return JsonResponse({"detail": "missing signature"}, status=401)
    if abs(time.time() - int(timestamp)) > settings.WEBHOOK_REPLAY_WINDOW_S:
        return JsonResponse({"detail": "timestamp outside the replay window"}, status=401)
    if not _signature_ok(secret, timestamp, request.body, signature):
        return JsonResponse({"detail": "bad signature"}, status=401)
    return None


@csrf_exempt
@require_POST
def provider_webhook(request: HttpRequest) -> JsonResponse:
    if (refused := _refused(request, settings.WEBHOOK_SIGNING_SECRET)) is not None:
        return refused
    try:
        data = json.loads(request.body)
        event_id = str(data["id"]).strip()
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"detail": "body must be JSON with an id"}, status=400)
    if not event_id:
        return JsonResponse({"detail": "body must be JSON with an id"}, status=400)

    with transaction.atomic():
        payload, created = RawPayload.objects.get_or_create(
            source=EventSource.WEBHOOK, external_id=event_id[:255],
            defaults={"body": request.body.decode("utf-8", "replace")},
        )  # fmt: skip
        if created:
            from workers.tasks.events import normalize_payload

            transaction.on_commit(partial(normalize_payload.delay, str(payload.pk)))
    return JsonResponse({"status": "accepted" if created else "duplicate", "id": event_id},
                        status=202 if created else 200)  # fmt: skip


@csrf_exempt
@require_POST
def mta_mail(request: HttpRequest) -> JsonResponse:
    """One message Postfix delivered to the bounce/FBL domain, as raw RFC 5322 bytes.

    Stored and evaluated in the request, so a 2xx means it is safely recorded;
    anything else makes the forwarder exit EX_TEMPFAIL and Postfix retry.
    Re-posting the same message records nothing twice.
    """
    if (refused := _refused(request, settings.MTA_INGEST_SECRET)) is not None:
        return refused
    if not request.body:
        return JsonResponse({"detail": "empty message"}, status=400)
    from workers.services.events import ingest_mail

    outcomes = ingest_mail(request.body)
    return JsonResponse({"status": "recorded",
                         "events": [{"type": o.event.type, "created": o.created} for o in outcomes]})  # fmt: skip


@csrf_exempt
@require_POST
def mta_log(request: HttpRequest) -> JsonResponse:
    """{"lines": ["<syslog or ISO timestamp> host postfix/smtp[pid]: ...", ...]}"""
    if (refused := _refused(request, settings.MTA_INGEST_SECRET)) is not None:
        return refused
    try:
        lines = json.loads(request.body)["lines"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"detail": "body must be JSON with a lines list"}, status=400)
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        return JsonResponse({"detail": "lines must be a list of strings"}, status=400)
    if len(lines) > MAX_LOG_LINES:
        return JsonResponse({"detail": f"at most {MAX_LOG_LINES} lines per request"}, status=413)
    from workers.services.events import ingest_log_lines

    return JsonResponse({"status": "recorded", **ingest_log_lines(lines)})
