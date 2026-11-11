"""VERP envelope senders: ``b-<job id>-<signature>@<return-path domain>``.

A bounce comes back to the address it was sent from, so encoding the job in the
envelope sender lets the event pipeline (§10.8) tie a DSN to exactly one job.
The HMAC stops a forged bounce from naming an arbitrary job.

The signature uses SECRET_KEY (Django ``salted_hmac``). Rotating SECRET_KEY
invalidates addresses already in flight — bounces arriving after a rotation will
not match and go to the operator review queue (§10.8.2 "unknown DSN").
"""

from __future__ import annotations

import hmac
import re
import uuid

from django.utils.crypto import salted_hmac

_SALT = "delivery.verp.v1"
_RE = re.compile(r"^b-([0-9a-f]{32})-([0-9a-f]{16})@", re.IGNORECASE)


def _sign(job_hex: str) -> str:
    return salted_hmac(_SALT, job_hex, algorithm="sha256").hexdigest()[:16]


def verp_address(job_id: uuid.UUID, return_path_domain: str) -> str:
    return f"b-{job_id.hex}-{_sign(job_id.hex)}@{return_path_domain}"


def parse_verp(address: str) -> uuid.UUID | None:
    """The job id an envelope address names, or None if absent or forged."""
    match = _RE.match(address.strip().strip("<>"))
    if not match:
        return None
    job_hex, signature = match.group(1).lower(), match.group(2).lower()
    if not hmac.compare_digest(_sign(job_hex), signature):
        return None
    return uuid.UUID(hex=job_hex)
