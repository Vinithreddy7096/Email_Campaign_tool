"""Campaign preflight (BUILD.md §10.6.3, spec §5.1 step 4).

``run(campaign)`` returns a ``Preflight``: findings plus the computed audience.
Approval reuses the same audience to build the snapshot, so what was checked is
exactly what is frozen.

Findings are FAIL (blocks submission/approval), WARN, or INFO. Suppressed
recipients are excluded and counted, never a failure (§10.6.3).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

from campaigns.models import Campaign
from common.models import Stream
from consent.evaluation import marketing_consent_bulk
from contacts import segments
from contacts.models import Contact
from domains.models import SendingDomain
from email_templates import preflight as template_preflight
from email_templates.models import EmailTemplate, TemplateVersion
from email_templates.rendering import RenderError, render
from suppression.models import Suppression

FAIL, WARN, INFO = "FAIL", "WARN", "INFO"

_CATEGORY_FOR_PURPOSE = {
    Stream.MARKETING: EmailTemplate.Category.MARKETING,
    Stream.TRANSACTIONAL: EmailTemplate.Category.TRANSACTIONAL,
}


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Audience:
    total: int = 0
    excluded_suppressed: int = 0
    # (contact_id, email_hash, consent_record_id or None), in contact-id order
    recipients: list[tuple[uuid.UUID, str, uuid.UUID | None]] = field(default_factory=list)
    missing_consent: Counter[str] = field(default_factory=Counter)

    def counts(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "excluded_suppressed": self.excluded_suppressed,
            "recipients": len(self.recipients),
            "missing_consent": dict(self.missing_consent),
        }


@dataclass
class Preflight:
    findings: list[Finding]
    audience: Audience
    samples: list[dict[str, Any]]

    @property
    def failed(self) -> bool:
        return any(f.level == FAIL for f in self.findings)

    def report(self) -> dict[str, Any]:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "audience": self.audience.counts(),
            "samples": self.samples,
        }


def contact_merge_data(contact: Contact) -> dict[str, Any]:
    """Merge data for one recipient. Named fields win over free-form attributes."""
    return {
        **(contact.attributes or {}),
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "email": contact.email,
    }


def compute_audience(campaign: Campaign) -> Audience:
    """Live list members (narrowed by the segment, if any), minus suppressed;
    marketing members need consent. A segment never bypasses either check.

    Raises ValidationError if the segment's rules are invalid."""
    members = Contact.objects.live().filter(memberships__contact_list=campaign.audience_list)
    if campaign.audience_segment_id is not None:
        members = members.filter(segments.to_q(campaign.audience_segment.definition))
    rows = list(members.order_by("id").values_list("id", "email", "email_hash"))
    audience = Audience(total=len(rows))
    pairs = [(h, (email or "").rpartition("@")[2]) for _, email, h in rows]
    blocked = Suppression.objects.blocked_among(pairs, campaign.purpose, campaign.audience_list)
    eligible = [(cid, h) for cid, _, h in rows if h not in blocked]
    audience.excluded_suppressed = len(rows) - len(eligible)

    if campaign.purpose != Stream.MARKETING:
        audience.recipients = [(cid, h, None) for cid, h in eligible]
        return audience
    consent = marketing_consent_bulk([cid for cid, _ in eligible], campaign.audience_list)
    for cid, h in eligible:
        verdict = consent[cid]
        if verdict.granted:
            audience.recipients.append((cid, h, verdict.record_id))
        else:
            audience.missing_consent[verdict.code] += 1
    return audience


def _template_findings(campaign: Campaign, version: TemplateVersion) -> list[Finding]:
    out: list[Finding] = []
    if version.status != TemplateVersion.Status.APPROVED:
        out.append(Finding("TEMPLATE_NOT_APPROVED", FAIL, f"{version} is {version.status}."))
    if version.template.category != _CATEGORY_FOR_PURPOSE.get(Stream(campaign.purpose)):
        out.append(Finding("TEMPLATE_PURPOSE_MISMATCH", FAIL,
                           f"{version} is a {version.template.category} template; this campaign "
                           f"is {campaign.purpose}."))  # fmt: skip
    # Re-run the template's own preflight on the chosen version (§10.6.3).
    for f in template_preflight.run(version):
        out.append(Finding(f"TEMPLATE_{f.code}", f.level, f.message))
    return out


def _sender_findings(campaign: Campaign) -> list[Finding]:
    sender = campaign.sender_identity
    domain = sender.domain
    out: list[Finding] = []
    from_domain = sender.from_address.rpartition("@")[2]
    if not (from_domain == domain.domain or from_domain.endswith(f".{domain.domain}")):
        out.append(Finding("SENDER_NOT_ALIGNED", FAIL,
                           f"From domain {from_domain} is not {domain.domain} or a subdomain."))  # fmt: skip
    if domain.status not in (SendingDomain.Status.VERIFIED, SendingDomain.Status.ACTIVE):
        out.append(Finding("SENDER_DOMAIN_NOT_VERIFIED", FAIL, f"{domain} is {domain.status}."))
    if domain.dmarc_verified_at is None:
        out.append(Finding("DMARC_NOT_VERIFIED", FAIL,
                           f"The DNS checker has not verified DMARC for {domain}."))  # fmt: skip
    if domain.stream != campaign.purpose:
        out.append(Finding("STREAM_MISMATCH", FAIL,
                           f"{domain} is a {domain.stream} domain; this campaign is {campaign.purpose}."))  # fmt: skip
    if not sender.is_active:
        out.append(Finding("SENDER_INACTIVE", FAIL, f"Sender {sender.from_address} is inactive."))
    return out


def _segment_findings(campaign: Campaign, audience: Audience) -> list[Finding]:
    segment = campaign.audience_segment
    if segment is None:
        return []
    if segment.contact_list_id != campaign.audience_list_id:
        return [Finding("SEGMENT_LIST_MISMATCH", FAIL,
                        f"Segment {segment.name!r} belongs to another list.")]  # fmt: skip
    return [Finding("SEGMENT_APPLIED", INFO,
                    f"Segment {segment.name!r} selects {audience.total} member(s) of "
                    f"{campaign.audience_list}.")]  # fmt: skip


def _audience_findings(campaign: Campaign, audience: Audience) -> list[Finding]:
    lst = campaign.audience_list
    out: list[Finding] = []
    if lst.archived_at is not None:
        out.append(Finding("LIST_ARCHIVED", FAIL, f"List {lst} is archived."))
    if lst.stream != campaign.purpose:
        out.append(Finding("LIST_STREAM_MISMATCH", FAIL,
                           f"List {lst} is a {lst.stream} list; this campaign is {campaign.purpose}."))  # fmt: skip
    missing = sum(audience.missing_consent.values())
    if missing:
        detail = ", ".join(f"{n} {code}" for code, n in sorted(audience.missing_consent.items()))
        out.append(Finding("MISSING_CONSENT", FAIL,
                           f"{missing} marketing recipient(s) lack valid consent ({detail})."))  # fmt: skip
    if audience.excluded_suppressed:
        out.append(Finding("SUPPRESSED_EXCLUDED", INFO,
                           f"{audience.excluded_suppressed} suppressed recipient(s) will be excluded."))  # fmt: skip
    if not audience.recipients and not missing:
        out.append(Finding("EMPTY_AUDIENCE", FAIL, "No recipients remain after exclusions."))
    return out


def _samples(
    campaign: Campaign, version: TemplateVersion, audience: Audience
) -> tuple[list[dict[str, Any]], list[Finding]]:
    n = settings.CAMPAIGN_PREFLIGHT_SAMPLE_SIZE
    ids = [cid for cid, _, _ in audience.recipients[:n]]
    contacts = {c.pk: c for c in Contact.objects.filter(pk__in=ids)}
    samples: list[dict[str, Any]] = []
    findings: list[Finding] = []
    for cid in ids:
        try:
            rendered = render(version, contact_merge_data(contacts[cid]),
                              template_preflight.preview_system_data(), require_approved=False)  # fmt: skip
        except RenderError as exc:
            samples.append({"contact_id": str(cid), "error": str(exc)})
            findings.append(Finding("SAMPLE_RENDER_FAILED", FAIL,
                                    f"Rendering for contact {cid} failed: {exc}"))  # fmt: skip
        else:
            samples.append({"contact_id": str(cid), "subject": rendered.subject,
                            "content_hash": rendered.content_hash})  # fmt: skip
    return samples, findings


def run(campaign: Campaign) -> Preflight:
    version = TemplateVersion.objects.select_related("template").get(
        pk=campaign.template_version_id
    )
    try:
        audience = compute_audience(campaign)
    except ValidationError as exc:
        # Invalid segment rules: nobody is selected, and the campaign cannot pass.
        audience = Audience()
        segment_findings = [Finding("SEGMENT_INVALID", FAIL, "; ".join(exc.messages))]
    else:
        segment_findings = _segment_findings(campaign, audience)
    findings = [
        *_template_findings(campaign, version),
        *_sender_findings(campaign),
        *segment_findings,
        *_audience_findings(campaign, audience),
    ]
    samples, sample_findings = _samples(campaign, version, audience)
    findings.extend(sample_findings)
    return Preflight(findings, audience, samples)
