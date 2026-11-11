"""Template lifecycle: drafts, submission, approval, retirement (BUILD.md §10.5.1).

Admin and any future API call these, so permission checks, preflight and
audit happen in one place.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from . import audit, preflight
from .models import EmailTemplate, TemplateVersion

Status = TemplateVersion.Status


class InvalidTransition(Exception):
    pass


class PreflightFailed(Exception):
    def __init__(self, findings):
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings if f.level == preflight.FAIL))


def _require(actor, perm):
    if actor is None or not actor.has_perm(f"email_templates.{perm}"):
        raise PermissionDenied(f"Missing permission email_templates.{perm}")


def _summary(version):
    return {"status": version.status, "checksum": version.checksum, "number": version.number}


def _locked(version):
    return TemplateVersion.objects.select_for_update().select_related("template").get(pk=version.pk)


@transaction.atomic
def create_template(
    actor,
    *,
    name,
    category,
    description="",
    subject,
    html_body,
    text_body,
    variables=None,
    sample_data=None,
):
    _require(actor, "add_emailtemplate")
    template = EmailTemplate.objects.create(
        name=name, category=category, description=description, owner=actor
    )
    audit.record(
        actor=actor,
        action="email_template.create",
        obj=template,
        after={"name": name, "category": category},
    )
    version = save_draft(
        actor,
        TemplateVersion(
            template=template,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            variables=variables or {},
            sample_data=sample_data or {},
        ),
    )
    return template, version


@transaction.atomic
def save_draft(actor, version):
    """Create a new draft (no pk) or save changes to an existing draft."""
    version.full_clean(exclude=["template", "number", "created_by", "checksum", "status"])
    if version.pk is None:
        _require(actor, "add_templateversion")
        EmailTemplate.objects.select_for_update().get(pk=version.template_id)
        last = version.template.versions.aggregate(n=Max("number"))["n"] or 0
        version.number = last + 1
        version.created_by = actor
        version.status = Status.DRAFT
        version.save()
        audit.record(
            actor=actor,
            action="email_template.version.create",
            obj=version,
            after=_summary(version),
        )
        return version

    _require(actor, "change_templateversion")
    current = _locked(version)
    if current.status != Status.DRAFT:
        raise InvalidTransition(f"{current} is {current.status}; only drafts can be edited.")
    before = _summary(current)
    version.status = Status.DRAFT
    version.save()
    if before["checksum"] != version.checksum:
        audit.record(
            actor=actor,
            action="email_template.version.edit",
            obj=version,
            before=before,
            after=_summary(version),
        )
    return version


@transaction.atomic
def new_version_from(actor, version):
    """Copy any version's content into a new draft of the same template."""
    source = TemplateVersion.objects.get(pk=version.pk)
    return save_draft(actor, TemplateVersion(template=source.template, **source.content()))


def submit(actor, version):
    """Run preflight and move a draft to PENDING_APPROVAL.

    The preflight report is saved even when it fails, so the author can see it.
    """
    _require(actor, "submit_templateversion")
    with transaction.atomic():
        version = _locked(version)
        if version.status != Status.DRAFT:
            raise InvalidTransition(f"{version} is {version.status}; only drafts can be submitted.")
        failed = _store_preflight(version)
        if not failed:
            before = _summary(version)
            version.status = Status.PENDING_APPROVAL
            version.submitted_by = actor
            version.submitted_at = timezone.now()
            version.save()
            audit.record(
                actor=actor,
                action="email_template.version.submit",
                obj=version,
                before=before,
                after=_summary(version),
            )
    if failed:
        raise PreflightFailed(failed)
    return version


def _store_preflight(version):
    """Run preflight, save the report; return the findings if any FAIL, else None."""
    findings = preflight.run(version)
    version.preflight_report = [f.as_dict() for f in findings]
    version.save(update_fields=["preflight_report"])
    return findings if preflight.has_failures(findings) else None


def _review(actor, version, *, to_status, note, action):
    _require(actor, "approve_templateversion")
    if to_status != Status.APPROVED and not note.strip():
        raise ValidationError("A note is required when rejecting or requesting changes.")
    with transaction.atomic():
        version = _locked(version)
        if version.status != Status.PENDING_APPROVAL:
            raise InvalidTransition(
                f"{version} is {version.status}; only pending versions can be reviewed."
            )
        # Re-check at approval so the stored report matches the frozen content.
        failed = _store_preflight(version) if to_status == Status.APPROVED else None
        if not failed:
            before = _summary(version)
            version.status = to_status
            version.reviewed_by = actor
            version.reviewed_at = timezone.now()
            version.review_note = note
            version.save()
            audit.record(
                actor=actor,
                action=action,
                obj=version,
                before=before,
                after={**_summary(version), "note": note},
            )
    if failed:
        raise PreflightFailed(failed)
    return version


def approve(actor, version, note=""):
    return _review(
        actor,
        version,
        to_status=Status.APPROVED,
        note=note,
        action="email_template.version.approve",
    )


def reject(actor, version, note):
    return _review(
        actor, version, to_status=Status.REJECTED, note=note, action="email_template.version.reject"
    )


def request_changes(actor, version, note):
    return _review(
        actor,
        version,
        to_status=Status.CHANGES_REQUESTED,
        note=note,
        action="email_template.version.request_changes",
    )


@transaction.atomic
def retire(actor, version, note=""):
    _require(actor, "approve_templateversion")
    version = _locked(version)
    if version.status != Status.APPROVED:
        raise InvalidTransition(
            f"{version} is {version.status}; only approved versions can be retired."
        )
    before = _summary(version)
    version.status = Status.RETIRED
    version.save(update_fields=["status"])
    audit.record(
        actor=actor,
        action="email_template.version.retire",
        obj=version,
        before=before,
        after={**_summary(version), "note": note},
    )
    return version
