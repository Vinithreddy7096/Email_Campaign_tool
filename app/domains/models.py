"""Sending domains, IPs, pools, sender identities (§6.2).

PROVISIONAL — written without the §6.2 field list; see docs/OPEN_QUESTIONS.md
Q-DATA-1. Nothing here sends mail. In MOCK_MODE the delivery service must ignore
pool and IP routing held in these tables (§5.3 item 1).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models

from common.email import normalize_domain
from common.models import Stream, TimestampedModel


def _default_dkim_selector() -> str:
    return settings.DKIM_SELECTOR_DEFAULT


class IpPool(TimestampedModel):
    name = models.SlugField(max_length=64, unique=True)
    stream = models.CharField(max_length=16, choices=Stream.choices)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "domains_ip_pool"

    def __str__(self) -> str:
        return self.name


class SendingIp(TimestampedModel):
    class Status(models.TextChoices):
        WARMING = "WARMING", "Warming"
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        RETIRED = "RETIRED", "Retired"

    address = models.GenericIPAddressField(unique=True)
    # Must match forward DNS (FCrDNS). The PTR record itself is set at the
    # hosting provider — Q-HOST-1.
    ptr_hostname = models.CharField(max_length=253)
    pool = models.ForeignKey(IpPool, on_delete=models.PROTECT, related_name="ips")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.WARMING)

    class Meta:
        db_table = "domains_sending_ip"

    def __str__(self) -> str:
        return self.address

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.ptr_hostname = normalize_domain(self.ptr_hostname)
        super().save(*args, **kwargs)


class SendingDomain(TimestampedModel):
    class Status(models.TextChoices):
        PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending DNS verification"
        VERIFIED = "VERIFIED", "DNS verified"
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        RETIRED = "RETIRED", "Retired"

    domain = models.CharField(max_length=253, unique=True)
    stream = models.CharField(max_length=16, choices=Stream.choices)
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.PENDING_VERIFICATION
    )
    ip_pool = models.ForeignKey(
        IpPool, on_delete=models.PROTECT, null=True, blank=True, related_name="domains"
    )

    return_path_domain = models.CharField(max_length=253)
    tracking_domain = models.CharField(max_length=253, blank=True)
    dkim_selector = models.CharField(max_length=63, default=_default_dkim_selector)

    spf_verified_at = models.DateTimeField(null=True, blank=True)
    dkim_verified_at = models.DateTimeField(null=True, blank=True)
    dmarc_verified_at = models.DateTimeField(null=True, blank=True)
    last_dns_check_at = models.DateTimeField(null=True, blank=True)
    # The last DNS checker report, per record: [{check, status, detail, record}].
    # Shown in the console's domain page; the timestamps above stay the gate.
    last_dns_report = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "domains_sending_domain"
        constraints = [
            # A domain cannot go live until SPF, DKIM and DMARC all verify.
            models.CheckConstraint(
                condition=~models.Q(status="ACTIVE")
                | (
                    models.Q(spf_verified_at__isnull=False)
                    & models.Q(dkim_verified_at__isnull=False)
                    & models.Q(dmarc_verified_at__isnull=False)
                ),
                name="domains_sending_domain_active_requires_dns",
            ),
        ]

    def __str__(self) -> str:
        return self.domain

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.domain = normalize_domain(self.domain)
        self.return_path_domain = normalize_domain(self.return_path_domain)
        if self.tracking_domain:
            self.tracking_domain = normalize_domain(self.tracking_domain)
        # A domain must not borrow the other stream's IP reputation. Checked
        # here rather than as a constraint because it spans two tables.
        if self.ip_pool is not None and self.ip_pool.stream != self.stream:
            raise ValueError(
                f"{self.domain} is {self.stream} but pool {self.ip_pool.name} "
                f"is {self.ip_pool.stream}; streams must not share pools"
            )
        super().save(*args, **kwargs)

    @property
    def dns_verified(self) -> bool:
        return all((self.spf_verified_at, self.dkim_verified_at, self.dmarc_verified_at))


class SenderIdentity(TimestampedModel):
    """A From identity. The address is always on its own verified sending domain,
    so a From on a domain we do not control cannot be expressed (§0.2)."""

    domain = models.ForeignKey(SendingDomain, on_delete=models.PROTECT, related_name="senders")
    local_part = models.CharField(max_length=64)
    display_name = models.CharField(max_length=128)
    reply_to = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "domains_sender_identity"
        verbose_name_plural = "sender identities"
        constraints = [
            models.UniqueConstraint(
                fields=["domain", "local_part"], name="domains_sender_identity_address_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} <{self.from_address}>"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.local_part = self.local_part.strip().lower()
        if not self.local_part or "@" in self.local_part:
            raise ValueError("local_part must be the part before '@', without the domain")
        super().save(*args, **kwargs)

    @property
    def from_address(self) -> str:
        return f"{self.local_part}@{self.domain.domain}"
