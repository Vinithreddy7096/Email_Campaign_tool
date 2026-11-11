"""Segment authoring. Same permission checks and audit records from the
console, the admin and tests. Rules: ``contacts.segments``."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from audit.models import AuditEvent
from contacts.models import ContactList, Segment


def _require(actor: Any, perm: str) -> None:
    if not actor.has_perm(f"contacts.{perm}"):
        raise PermissionDenied(f"You need contacts.{perm} to do that.")


@transaction.atomic
def create_segment(actor: Any, *, contact_list: ContactList, name: str,
                   definition: dict[str, Any]) -> Segment:  # fmt: skip
    _require(actor, "add_segment")
    if contact_list.archived_at is not None:
        raise ValidationError(f"List {contact_list} is archived.")
    segment = Segment(contact_list=contact_list, name=name.strip(), definition=definition)
    segment.full_clean()
    segment.save()
    AuditEvent.record("segment.create", actor=actor, target=segment,
                      after={"list": str(contact_list.pk), "name": segment.name,
                             "definition": definition})  # fmt: skip
    return segment


@transaction.atomic
def update_segment(actor: Any, segment: Segment, *, name: str | None = None,
                   definition: dict[str, Any] | None = None) -> Segment:  # fmt: skip
    """Editing a segment changes who a not-yet-approved campaign would reach;
    approval re-runs preflight, and an approved campaign keeps its snapshot."""
    _require(actor, "change_segment")
    current = Segment.objects.select_for_update().get(pk=segment.pk)
    before = {"name": current.name, "definition": current.definition}
    if name is not None:
        current.name = name.strip()
    if definition is not None:
        current.definition = definition
    current.full_clean()
    current.save()
    AuditEvent.record("segment.edit", actor=actor, target=current, before=before,
                      after={"name": current.name, "definition": current.definition})  # fmt: skip
    return current
