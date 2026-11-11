"""Campaign API (BUILD.md §10.6.5). Thin: every action is a campaigns.services call.

Token scopes (a token's empty scope list grants nothing):
  campaigns:read     GET detail and metrics
  campaigns:write    create, submit, schedule, pause
  campaigns:approve  approve — separate because API tokens bypass MFA, and
                     approval is the step the spec reserves for approvers
Session (browser) callers need only the role permission; MFA already applies.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from accounts.models import ApiToken
from campaigns import services
from campaigns.models import Campaign
from common.models import Stream
from contacts.models import ContactList, Segment
from domains.models import SenderIdentity
from email_templates.models import TemplateVersion
from workers.services.delivery import state_counts


class CampaignCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    purpose = serializers.ChoiceField(choices=Stream.choices)
    sender_identity = serializers.PrimaryKeyRelatedField(queryset=SenderIdentity.objects.all())
    template_version = serializers.PrimaryKeyRelatedField(queryset=TemplateVersion.objects.all())
    audience_list = serializers.PrimaryKeyRelatedField(queryset=ContactList.objects.all())
    audience_segment = serializers.PrimaryKeyRelatedField(
        queryset=Segment.objects.all(), required=False, allow_null=True
    )
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)


def _body(c: Campaign) -> dict[str, Any]:
    return {
        "id": str(c.pk),
        "name": c.name,
        "purpose": c.purpose,
        "status": c.status,
        "sender": c.sender_identity.from_address,
        "template_version": c.template_version_id,
        "audience_list": str(c.audience_list_id),
        "audience_segment": str(c.audience_segment_id) if c.audience_segment_id else None,
        "scheduled_at": c.scheduled_at,
        "reviewed_by": getattr(c.reviewed_by, "email", None),
        "reviewed_at": c.reviewed_at,
        "preflight": c.preflight_report or None,
    }


def _scope(request: Request, scope: str) -> None:
    token = request.auth
    if isinstance(token, ApiToken) and not token.has_scope(scope):
        raise PermissionDenied(f"Token lacks scope {scope}")


def _call(func: Any, *args: Any, ok: int = status.HTTP_200_OK, **kwargs: Any) -> Response:
    try:
        result = func(*args, **kwargs)
    except PermissionDenied as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    except services.InvalidTransition as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except services.PreflightFailed as exc:
        return Response({"detail": "Preflight failed",
                         "findings": [f.as_dict() for f in exc.findings]},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)  # fmt: skip
    except services.ScheduleRejected as exc:
        return Response({"detail": "Schedule rejected", "reasons": exc.reasons},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)  # fmt: skip
    except ValidationError as exc:
        return Response({"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
    campaign = getattr(result, "campaign", result)
    body = _body(campaign)
    if hasattr(result, "warnings"):
        body["warnings"] = result.warnings
    if campaign.status in (Campaign.Status.PAUSED, Campaign.Status.CANCELED):
        body["note"] = services.ACCEPTED_STILL_COMPLETE
    return Response(body, status=ok)


def _get(pk: str) -> Campaign:
    return get_object_or_404(Campaign.objects.select_related("sender_identity__domain"), pk=pk)


@api_view(["POST"])
def create(request: Request) -> Response:
    _scope(request, "campaigns:write")
    data = CampaignCreateSerializer(data=request.data)
    data.is_valid(raise_exception=True)
    return _call(services.create, request.user, ok=status.HTTP_201_CREATED, **data.validated_data)


@api_view(["GET"])
def detail(request: Request, pk: str) -> Response:
    _scope(request, "campaigns:read")
    if not request.user.has_perm("campaigns.view_campaign"):
        raise PermissionDenied("Missing permission campaigns.view_campaign")
    return Response(_body(_get(pk)))


@api_view(["POST"])
def submit(request: Request, pk: str) -> Response:
    _scope(request, "campaigns:write")
    return _call(services.submit, request.user, _get(pk))


@api_view(["POST"])
def approve(request: Request, pk: str) -> Response:
    _scope(request, "campaigns:approve")
    return _call(services.approve, request.user, _get(pk), request.data.get("note", ""))


@api_view(["POST"])
def schedule(request: Request, pk: str) -> Response:
    _scope(request, "campaigns:write")
    at = serializers.DateTimeField(allow_null=True).to_internal_value(request.data["scheduled_at"]) \
        if request.data.get("scheduled_at") else None  # fmt: skip
    return _call(services.schedule, request.user, _get(pk), at)


@api_view(["POST"])
def pause(request: Request, pk: str) -> Response:
    _scope(request, "campaigns:write")
    return _call(
        services.pause, request.user, _get(pk), request.data.get("reason", "paused via API")
    )


def _delivery_stats(campaign: Campaign) -> dict[str, Any] | None:
    stats = getattr(campaign, "delivery_stats", None)
    if stats is None:
        return None
    return {f: getattr(stats, f) for f in ("delivered", "deferred", "hard_bounces", "soft_bounces",
                                           "complaints", "unsubscribes", "unknown")} | {
        "updated_at": stats.updated_at}  # fmt: skip


@api_view(["GET"])
def metrics(request: Request, pk: str) -> Response:
    """Audience numbers from the approval snapshot and job counts by state.
    Recipient-side outcomes (delivered, bounced, complained) come with §10.8.3."""
    _scope(request, "campaigns:read")
    if not request.user.has_perm("campaigns.view_campaign"):
        raise PermissionDenied("Missing permission campaigns.view_campaign")
    campaign = _get(pk)
    snapshot = getattr(campaign, "snapshot", None)
    return Response({
        "id": str(campaign.pk),
        "status": campaign.status,
        "audience": None if snapshot is None else {
            "total": snapshot.audience_total,
            "excluded_suppressed": snapshot.excluded_suppressed,
            "recipients": snapshot.recipient_count,
        },
        # Job states: ACCEPTED means the MTA took the message.
        "jobs": state_counts(campaign.pk),
        # Recipient-side outcomes from the event pipeline (§10.8.3), refreshed
        # every few minutes.
        "delivery": _delivery_stats(campaign),
    })  # fmt: skip
