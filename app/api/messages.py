"""Transactional API (BUILD.md §10.6.6).

POST /api/v1/messages        scope messages:send
    {"template": "Order receipt", "sender": "no-reply@notify.example.com",
     "to": "dana@example.org", "data": {"order_id": "A-1"}, "idempotency_key": "order-A-1"}
    202 accepted (new) · 200 (same key and body: the original message)
    409 same key, different body · 422 rejected (code + field) · 429 rate limited

GET /api/v1/messages/{id}    scope messages:send or messages:read
    The message's state, attempts and delivery events. Only messages sent by
    the same user's tokens are visible.

Token only: browser sessions are not accepted here.
"""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.models import ApiToken
from delivery.models import PolicyDecision, RecipientJob
from workers.services import transactional


def _token(request: Request, *scopes: str) -> ApiToken | Response:
    token = request.auth
    if not isinstance(token, ApiToken):
        return Response(
            {"detail": "An API token is required."}, status=status.HTTP_401_UNAUTHORIZED
        )
    if not any(token.has_scope(s) for s in scopes):
        return Response({"detail": f"Token lacks scope {' or '.join(scopes)}."},
                        status=status.HTTP_403_FORBIDDEN)  # fmt: skip
    return token


def _body(job: RecipientJob, client_key: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(job.pk),
        "status": job.state,
        "template": job.template_version.template.name,
        "version": job.template_version.number,
        "created_at": job.created_at,
        "attempts": job.attempts,
    }
    if client_key:
        out["idempotency_key"] = client_key
    return out


@api_view(["POST"])
def send(request: Request) -> Response:
    token = _token(request, "messages:send")
    if isinstance(token, Response):
        return token
    d = request.data if isinstance(request.data, dict) else {}
    key = str(d.get("idempotency_key") or request.headers.get("Idempotency-Key") or "")
    try:
        result = transactional.submit_message(
            token, template=str(d.get("template") or ""), sender=str(d.get("sender") or ""),
            to=d.get("to") or "", data=d.get("data", {}), idempotency_key=key,
        )  # fmt: skip
    except transactional.MessageRejected as exc:
        return Response({"code": exc.code, "detail": exc.detail, "field": exc.field},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)  # fmt: skip
    except transactional.IdempotencyConflict as exc:
        return Response(
            {"code": "IDEMPOTENCY_CONFLICT", "detail": str(exc)}, status=status.HTTP_409_CONFLICT
        )
    except transactional.RateLimited as exc:
        return Response({"code": "RATE_LIMITED", "detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS,
                        headers={"Retry-After": str(exc.retry_after)})  # fmt: skip
    body = _body(result.job, key)
    if result.warnings:
        body["warnings"] = result.warnings
    return Response(body, status=status.HTTP_202_ACCEPTED if result.created else status.HTTP_200_OK)


@api_view(["GET"])
def detail(request: Request, pk: str) -> Response:
    token = _token(request, "messages:send", "messages:read")
    if isinstance(token, Response):
        return token
    own_tokens = [
        str(t) for t in ApiToken.objects.filter(user=token.user).values_list("pk", flat=True)
    ]
    job = get_object_or_404(
        RecipientJob.objects.select_related("template_version__template"),
        pk=pk,
        api_client_ref__in=own_tokens,
    )
    body = _body(job)
    message = getattr(job, "message", None)
    body["message_id"] = message.message_id if message else None
    body["accepted_at"] = message.accepted_at if message else None
    body["events"] = [
        {"type": e.type, "code": e.enhanced_code or e.smtp_code, "occurred_at": e.occurred_at}
        for e in job.events.order_by("occurred_at")
    ]
    body["history"] = [
        {"state": t.to_state, "code": t.code, "at": t.created_at}
        for t in job.transitions.order_by("created_at")
    ]
    if job.state not in RecipientJob.TERMINAL:
        last = PolicyDecision.objects.filter(job_ref=str(job.pk)).order_by("-created_at").first()
        if last is not None and last.outcome == PolicyDecision.Outcome.HOLD:
            # Why it is waiting (a pause, a rate limit, warm-up) and when it is asked again.
            body["held"] = {"reasons": [{"code": r["code"], "detail": r["detail"]} for r in last.reasons],
                            "next_attempt_at": job.next_attempt_at}  # fmt: skip
    return Response(body)
