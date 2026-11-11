"""email_templates wired into the platform (BUILD.md §10.5.5): platform roles grant
its permissions, and its audit events land in the immutable audit log."""

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from email_templates import services
from email_templates.models import TemplateVersion
from email_templates.rendering import render

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

HTML = (
    '<p>Hi {{ first_name }}</p><p><a href="https://example.org/x">Offer</a></p>'
    '<p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>'
)
TEXT = "Hi {{ first_name }}\nUnsubscribe: {{ unsubscribe_url }}\n"


@pytest.fixture
def user_with(django_user_model):
    def make(email, *codes):
        user = django_user_model.objects.create_user(email, "pw", is_staff=True)
        for code in codes:
            UserRole.grant(user, code, by=None)
        return django_user_model.objects.get(pk=user.pk)

    return make


def _create(author):
    return services.create_template(
        author,
        name="Newsletter",
        category="marketing",
        subject="News for {{ first_name }}",
        html_body=HTML,
        text_body=TEXT,
        variables={"first_name": {"required": True}},
        sample_data={"first_name": "Ada"},
    )


def test_roles_drive_the_full_approval_flow(user_with):
    manager = user_with("manager@example.com", Role.Code.CAMPAIGN_MANAGER)
    approver = user_with("approver@example.com", Role.Code.APPROVER)

    _, version = _create(manager)
    services.submit(manager, version)
    with pytest.raises(PermissionDenied):
        services.approve(manager, version)  # authors cannot approve
    services.approve(approver, version)

    version.refresh_from_db()
    assert version.status == TemplateVersion.Status.APPROVED
    rendered = render(version, {"first_name": "Ada"}, {"unsubscribe_url": "https://u.example/t"})
    assert "Ada" in rendered.html and rendered.template_checksum == version.checksum


@pytest.mark.parametrize(
    ("code", "can_author", "can_submit", "can_approve"),
    [
        (Role.Code.SYSTEM_ADMIN, True, True, True),
        (Role.Code.CAMPAIGN_MANAGER, True, True, False),
        (Role.Code.CONTENT_EDITOR, True, False, False),
        (Role.Code.APPROVER, False, False, True),
        (Role.Code.ANALYST, False, False, False),
    ],
)
def test_role_permission_mapping(user_with, code, can_author, can_submit, can_approve):
    user = user_with(f"{code.lower()}@example.com", code)
    assert user.has_perm("email_templates.view_templateversion")
    assert user.has_perm("email_templates.add_templateversion") is can_author
    assert user.has_perm("email_templates.submit_templateversion") is can_submit
    assert user.has_perm("email_templates.approve_templateversion") is can_approve


def test_system_admin_holds_platform_permissions_but_not_django_auth(user_with):
    admin = user_with("sysadmin@example.com", Role.Code.SYSTEM_ADMIN)
    for perm in ("suppression.add_suppression", "domains.change_sendingdomain",
                 "delivery.add_sendpause", "audit.view_auditevent"):  # fmt: skip
        assert admin.has_perm(perm), perm
    assert not admin.has_perm("auth.change_group")
    manager = user_with("manager2@example.com", Role.Code.CAMPAIGN_MANAGER)
    assert not manager.has_perm("suppression.add_suppression")


def test_no_role_means_no_template_access(user_with):
    nobody = user_with("nobody@example.com")
    assert not nobody.has_perm("email_templates.view_templateversion")
    assert not nobody.has_module_perms("email_templates")


def test_revoking_a_role_takes_effect_on_the_same_object(user_with):
    manager = user_with("manager@example.com", Role.Code.CAMPAIGN_MANAGER)
    assert manager.has_perm("email_templates.submit_templateversion")
    UserRole.revoke(manager, Role.Code.CAMPAIGN_MANAGER, by=None)
    assert not manager.has_perm("email_templates.submit_templateversion")


def test_inactive_users_have_no_role_permissions(user_with):
    admin = user_with("admin@example.com", Role.Code.SYSTEM_ADMIN)
    admin.is_active = False
    assert not admin.has_perm("email_templates.approve_templateversion")


def test_lifecycle_lands_in_the_audit_log(user_with):
    manager = user_with("manager@example.com", Role.Code.CAMPAIGN_MANAGER)
    approver = user_with("approver@example.com", Role.Code.APPROVER)
    _, version = _create(manager)
    services.submit(manager, version)
    services.approve(approver, version)

    # A set, not created_at order: events in one clock tick share a timestamp.
    actions = set(
        AuditEvent.objects.filter(action__startswith="email_template").values_list(
            "action", "actor__email"
        )
    )
    assert actions == {
        ("email_template.create", "manager@example.com"),
        ("email_template.version.create", "manager@example.com"),
        ("email_template.version.submit", "manager@example.com"),
        ("email_template.version.approve", "approver@example.com"),
    }
    approve = AuditEvent.objects.get(action="email_template.version.approve")
    assert approve.target_type == "email_templates.TemplateVersion"
    assert approve.after["status"] == "APPROVED" and approve.after["checksum"] == version.checksum


def test_admin_edit_with_a_datetime_is_audited(login_verified, user_with):
    """The admin audits form.cleaned_data, which holds datetimes; plain JSON can't."""
    admin = user_with("admin@example.com", Role.Code.SYSTEM_ADMIN)
    template, _ = _create(admin)
    client = login_verified(admin)
    when = timezone.now().replace(microsecond=0)
    resp = client.post(
        reverse("admin:email_templates_emailtemplate_change", args=[template.pk]),
        {
            "name": "Newsletter",
            "category": "marketing",
            "description": "archived",
            "archived_at_0": when.strftime("%Y-%m-%d"),
            "archived_at_1": when.strftime("%H:%M:%S"),
            "versions-TOTAL_FORMS": "1",
            "versions-INITIAL_FORMS": "1",
            "versions-MIN_NUM_FORMS": "0",
            "versions-MAX_NUM_FORMS": "1000",
            "versions-0-id": str(template.versions.get().pk),
            "versions-0-template": str(template.pk),
        },
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    event = AuditEvent.objects.get(action="email_template.edit")
    assert event.after["archived_at"].startswith(when.strftime("%Y-%m-%d"))


def test_role_holder_sees_templates_in_admin(login_verified, user_with):
    manager = user_with("manager@example.com", Role.Code.CAMPAIGN_MANAGER)
    client = login_verified(manager)
    assert (
        client.get(reverse("admin:email_templates_templateversion_changelist")).status_code == 200
    )
    analyst_client = login_verified(user_with("analyst@example.com", Role.Code.ANALYST))
    add = reverse("admin:email_templates_templateversion_add")
    assert analyst_client.get(add).status_code == 403
