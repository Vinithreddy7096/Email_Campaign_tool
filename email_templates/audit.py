"""Adapter to the project's audit log (spec §5.2 Auditability, BUILD.md §10.5.5)."""

import logging

from django.utils.module_loading import import_string

from . import conf

logger = logging.getLogger("email_templates.audit")


def record(*, actor, action, obj, before=None, after=None):
    path = conf.get("AUDIT_FUNC")
    if path:
        import_string(path)(actor=actor, action=action, obj=obj, before=before, after=after)
        return
    logger.info(
        "audit action=%s object=%s:%s actor=%s before=%s after=%s",
        action,
        obj._meta.label,
        obj.pk,
        getattr(actor, "pk", actor),
        before,
        after,
    )
