"""
Apply a DNS report to a SendingDomain (§6.2 verification timestamps).

Rules:
- A check that passes (or only warns) sets its ``*_verified_at`` if unset.
- A check that fails clears it.
- A lookup error (UNKNOWN) changes nothing: a resolver timeout is not evidence.
- An ACTIVE domain whose SPF, DKIM or DMARC stops verifying is PAUSED — fail
  closed. Reactivation is a human decision; this never sets ACTIVE.
- A PENDING_VERIFICATION domain is promoted to VERIFIED only when every check,
  including bounce MX and reverse DNS, is acceptable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.models import AuditEvent
from domains.models import SendingDomain, SendingIp
from workers.services.dns_checks import DnsReport, Resolver, Status, check_domain

_TIMESTAMP_FOR_CHECK = {
    "spf": "spf_verified_at",
    "dkim": "dkim_verified_at",
    "dmarc": "dmarc_verified_at",
}


def _sending_ips(domain: SendingDomain) -> dict[str, str]:
    if domain.ip_pool_id is None:
        return {}
    ips = SendingIp.objects.filter(pool_id=domain.ip_pool_id).exclude(
        status=SendingIp.Status.RETIRED
    )
    return {ip.address: ip.ptr_hostname for ip in ips}


def verify_domain(
    domain: SendingDomain,
    resolve: Resolver,
    *,
    now: datetime | None = None,
    actor: Any = None,
    actor_label: str = "celery:domains.verify_all",
) -> DnsReport:
    """``actor`` is the user who asked for the check, if any; otherwise the
    audit event is attributed to ``actor_label``."""
    now = now or timezone.now()
    report = check_domain(
        resolve,
        domain=domain.domain,
        selector=domain.dkim_selector,
        return_path_domain=domain.return_path_domain,
        tracking_domain=domain.tracking_domain or None,
        sending_ips=_sending_ips(domain),
    )

    with transaction.atomic():
        domain = SendingDomain.objects.select_for_update().get(pk=domain.pk)
        before = {
            field: getattr(domain, field) is not None for field in _TIMESTAMP_FOR_CHECK.values()
        }
        status_before = domain.status

        for check, field in _TIMESTAMP_FOR_CHECK.items():
            status = report.get(check).status
            if status.acceptable and getattr(domain, field) is None:
                setattr(domain, field, now)
            elif status is Status.FAIL:
                setattr(domain, field, None)

        if domain.status == SendingDomain.Status.ACTIVE and not domain.dns_verified:
            domain.status = SendingDomain.Status.PAUSED
        elif domain.status == SendingDomain.Status.PENDING_VERIFICATION and report.ok:
            domain.status = SendingDomain.Status.VERIFIED

        domain.last_dns_check_at = now
        domain.last_dns_report = [
            {"check": r.check, "status": r.status.value, "detail": r.detail, "record": r.record}
            for r in report.results
        ]
        domain.save()

        after = {
            field: getattr(domain, field) is not None for field in _TIMESTAMP_FOR_CHECK.values()
        }
        if after != before or domain.status != status_before:
            AuditEvent.record(
                "domain.dns_check",
                actor=actor,
                actor_label="" if actor else actor_label,
                target=domain,
                before={"status": status_before, **before},
                after={"status": domain.status, **after},
                results={r.check: r.status.value for r in report.results},
            )
    return report
