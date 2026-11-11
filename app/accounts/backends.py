"""Grant Django model permissions from platform roles.

Components such as email_templates check ``user.has_perm("app.codename")``.
Rather than copying permission rows onto every user, a role implies a fixed set
of permissions, so a grant or revoke takes effect immediately and the mapping
lives in one reviewable place.

Mapping source: BUILD.md §10.5.4 for templates ("System Administrator: all").
SYSTEM_ADMIN holds every model permission in the platform's own apps — the
admin's view-only and no-delete rules still apply on top. Other roles get only
what is listed. CONTENT_EDITOR is not a spec role (Q-DATA-1); it gets
authoring rights but cannot submit.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.backends import BaseBackend

_TEMPLATE_VIEW = {"email_templates.view_emailtemplate", "email_templates.view_templateversion"}
_TEMPLATE_AUTHOR = _TEMPLATE_VIEW | {
    "email_templates.add_emailtemplate",
    "email_templates.change_emailtemplate",
    "email_templates.add_templateversion",
    "email_templates.change_templateversion",
}

# Apps whose every permission SYSTEM_ADMIN holds. Not django.contrib.auth:
# Django groups/permissions are not how this platform grants access.
PLATFORM_APPS = (
    "accounts",
    "domains",
    "contacts",
    "consent",
    "suppression",
    "email_templates",
    "campaigns",
    "delivery",
    "events",
    "audit",
)

# Campaigns (§10.6.2): managers build, submit, schedule and pause; approvers
# approve and may also pause (an emergency stop should not need the author).
_CAMPAIGN_VIEW = {"campaigns.view_campaign"}
_CAMPAIGN_MANAGE = _CAMPAIGN_VIEW | {
    "campaigns.add_campaign",
    "campaigns.change_campaign",
    "campaigns.submit_campaign",
    "campaigns.schedule_campaign",
    "campaigns.pause_campaign",
}

# Contacts: managers import files (with attestation) and build segments;
# everyone with a role may look. Erasure and suppression stay admin-only.
_CONTACT_VIEW = {
    "contacts.view_contact",
    "contacts.view_contactlist",
    "contacts.view_contactimport",
    "contacts.view_segment",
}
_CONTACT_MANAGE = _CONTACT_VIEW | {
    "contacts.add_contactimport",
    "contacts.add_segment",
    "contacts.change_segment",
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "CAMPAIGN_MANAGER": frozenset(
        _TEMPLATE_AUTHOR
        | {"email_templates.submit_templateversion"}
        | _CAMPAIGN_MANAGE
        | _CONTACT_MANAGE
    ),
    "APPROVER": frozenset(
        _TEMPLATE_VIEW
        | {"email_templates.approve_templateversion"}
        | _CAMPAIGN_VIEW
        | {"campaigns.approve_campaign", "campaigns.pause_campaign"}
        | _CONTACT_VIEW
    ),
    "CONTENT_EDITOR": frozenset(_TEMPLATE_AUTHOR | _CAMPAIGN_VIEW | _CONTACT_VIEW),
    "ANALYST": frozenset(_TEMPLATE_VIEW | _CAMPAIGN_VIEW | _CONTACT_VIEW),
}


def _platform_permissions() -> set[str]:
    from django.contrib.auth.models import Permission

    rows = Permission.objects.filter(content_type__app_label__in=PLATFORM_APPS).values_list(
        "content_type__app_label", "codename"
    )
    return {f"{app}.{codename}" for app, codename in rows}


class RolePermissionBackend(BaseBackend):
    """Authorization only — ``authenticate()`` always declines."""

    def get_all_permissions(self, user_obj: Any, obj: Any = None) -> set[str]:
        if not user_obj.is_active or user_obj.is_anonymous or obj is not None:
            return set()
        cache_name = "_role_perm_cache"
        if not hasattr(user_obj, cache_name):
            perms: set[str] = set()
            codes = user_obj.role_codes
            for code in codes:
                perms |= ROLE_PERMISSIONS.get(code, frozenset())
            if "SYSTEM_ADMIN" in codes:
                perms |= _platform_permissions()
            setattr(user_obj, cache_name, perms)
        return getattr(user_obj, cache_name)

    def has_perm(self, user_obj: Any, perm: str, obj: Any = None) -> bool:
        return perm in self.get_all_permissions(user_obj, obj)

    def has_module_perms(self, user_obj: Any, app_label: str) -> bool:
        return any(p.startswith(f"{app_label}.") for p in self.get_all_permissions(user_obj))
