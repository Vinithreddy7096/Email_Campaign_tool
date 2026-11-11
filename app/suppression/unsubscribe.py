"""Unsubscribe links (BUILD.md §10.8.4).

Built with the delivery pipeline because rendering needs a working
``unsubscribe_url``: a marketing message must never carry a broken one.

Token = Django-signed contact id + scope + campaign id. No expiry ("remain
functional"), and no timestamp: the token must be identical every time it is
made, because the delivery pipeline re-renders at handoff and requires the same
content hash it stored at render time. Scope is "MARKETING" — Q-EVT-4's default: one click leaves all
marketing from this organization. Applying it is idempotent:

- a STREAM=MARKETING suppression (UNSUBSCRIBE), and
- a WITHDRAWN marketing ConsentRecord, unless consent is already withdrawn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse

from common.models import Stream
from consent.evaluation import CONSENT_WITHDRAWN, marketing_consent
from consent.models import ConsentRecord
from contacts.models import Contact
from suppression.models import Suppression

log = logging.getLogger(__name__)

_SALT = "suppression.unsubscribe.v1"
SCOPE_MARKETING = "MARKETING"


@dataclass(frozen=True)
class UnsubscribeToken:
    contact_id: str
    scope: str
    campaign_id: str


def make_token(contact_id: Any, campaign_id: Any = "", scope: str = SCOPE_MARKETING) -> str:
    # Signer, not signing.dumps(): dumps() embeds a timestamp (see module doc).
    return signing.Signer(salt=_SALT).sign_object(
        {"c": str(contact_id), "s": scope, "k": str(campaign_id or "")}, compress=True
    )


def read_token(token: str) -> UnsubscribeToken | None:
    try:
        data = signing.Signer(salt=_SALT).unsign_object(token)
    except (signing.BadSignature, ValueError):
        return None
    if not isinstance(data, dict) or data.get("s") != SCOPE_MARKETING or not data.get("c"):
        return None
    return UnsubscribeToken(data["c"], data["s"], data.get("k", ""))


def unsubscribe_url(contact_id: Any, campaign_id: Any = "") -> str:
    path = reverse("unsubscribe", args=[make_token(contact_id, campaign_id)])
    return f"{settings.BASE_URL.rstrip('/')}{path}"


@transaction.atomic
def apply(token: UnsubscribeToken, *, one_click: bool, ip_address: str | None = None,
          user_agent: str = "") -> Contact | None:  # fmt: skip
    contact = Contact.objects.select_for_update().filter(pk=token.contact_id).first()
    if contact is None:
        return None
    source = f"unsubscribe:campaign={token.campaign_id or '-'}"
    from workers.services.events import record_unsubscribe_link

    try:
        # Telemetry only. It must never stand between a person and unsubscribing.
        with transaction.atomic():
            record_unsubscribe_link(token.contact_id, token.campaign_id, contact.email_hash,
                                    (contact.email or "").rpartition("@")[2])  # fmt: skip
    except Exception:
        log.exception("could not record unsubscribe telemetry for contact %s", token.contact_id)
    if contact.email:
        Suppression.suppress(contact.email, Suppression.Reason.UNSUBSCRIBE,
                             scope=Suppression.Scope.STREAM, stream=Stream.MARKETING,
                             source=source)  # fmt: skip
    if marketing_consent(contact.pk).code != CONSENT_WITHDRAWN:
        ConsentRecord.objects.create(
            contact=contact,
            stream=Stream.MARKETING,
            action=ConsentRecord.Action.WITHDRAWN,
            method=(ConsentRecord.Method.LIST_UNSUBSCRIBE if one_click
                    else ConsentRecord.Method.UNSUBSCRIBE_LINK),
            source=source,
            ip_address=ip_address,
            user_agent=user_agent[:1000],
        )  # fmt: skip
    return contact
