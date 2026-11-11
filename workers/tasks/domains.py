"""Periodic DNS re-verification of sending domains (maintenance queue)."""

from __future__ import annotations

import logging

from celery import shared_task

from domains.models import SendingDomain
from workers.services.dns_checks import dnspython_resolver
from workers.services.domain_verification import verify_domain

log = logging.getLogger(__name__)


@shared_task(name="domains.verify_all")
def verify_all_domains() -> dict[str, bool]:
    resolve = dnspython_resolver()
    results: dict[str, bool] = {}
    for domain in SendingDomain.objects.exclude(status=SendingDomain.Status.RETIRED):
        # One broken domain must not stop the others from being checked.
        try:
            results[domain.domain] = verify_domain(domain, resolve).ok
        except Exception:
            log.exception("DNS verification crashed for %s", domain.domain)
            results[domain.domain] = False
    return results
