"""Identity and access (§6.1): roles, API tokens, and the admin surface."""

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken, Role, UserRole
from audit.models import AuditEvent
from consent.models import ConsentRecord
from contacts.models import Contact

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user("ops@example.com", "pw-not-used-here")


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_superuser("root@example.com", "pw-not-used-here")


# ─── Roles ───────────────────────────────────────────────────────────────


def test_every_role_is_created_by_migration():
    assert set(Role.objects.values_list("code", flat=True)) == set(Role.Code.values)


def test_grant_and_revoke_are_idempotent_and_audited(user, staff):
    _, created = UserRole.grant(user, Role.Code.APPROVER, by=staff)
    _, again = UserRole.grant(user, Role.Code.APPROVER, by=staff)
    assert created and not again
    assert user.has_role(Role.Code.APPROVER)

    assert UserRole.revoke(user, Role.Code.APPROVER, by=staff)
    assert not UserRole.revoke(user, Role.Code.APPROVER, by=staff)
    assert not user.has_role(Role.Code.APPROVER)

    actions = sorted(AuditEvent.objects.values_list("action", flat=True))
    assert actions == ["role.grant", "role.revoke"]


def test_mfa_is_required_for_configured_roles(user, settings):
    settings.MFA_REQUIRED_ROLES = ["SYSTEM_ADMIN", "APPROVER"]
    UserRole.grant(user, Role.Code.ANALYST, by=None)
    assert not user.mfa_required
    UserRole.grant(user, Role.Code.APPROVER, by=None)
    assert user.mfa_required


# ─── API tokens ──────────────────────────────────────────────────────────


def test_issued_token_authenticates_and_secret_is_not_stored(user):
    token, plaintext = ApiToken.issue(user, "ci", scopes=["contacts:read"])
    assert plaintext.startswith(f"ept_{token.prefix}_")
    secret = plaintext.split("_", 2)[2]
    assert secret not in token.secret_hash
    found = ApiToken.authenticate(plaintext)
    assert found == token and found.has_scope("contacts:read")
    token.refresh_from_db()
    assert token.last_used_at is not None
    assert AuditEvent.objects.filter(action="api_token.issue").exists()


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p[:-1] + ("A" if p[-1] != "A" else "B"),  # wrong secret
        lambda p: "xyz" + p[3:],  # wrong tag
        lambda p: p.rsplit("_", 1)[0],  # no secret
        lambda p: p + "é",  # non-ASCII
        lambda p: "",
    ],
)
def test_malformed_or_wrong_tokens_are_rejected(user, mangle):
    _, plaintext = ApiToken.issue(user, "ci")
    assert ApiToken.authenticate(mangle(plaintext)) is None


def test_expired_revoked_and_inactive_tokens_are_rejected(user):
    expired, p1 = ApiToken.issue(user, "old", ttl_days=1)
    ApiToken.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(1))
    assert ApiToken.authenticate(p1) is None

    revoked, p2 = ApiToken.issue(user, "revoked")
    revoked.revoke(by=None)
    assert ApiToken.authenticate(p2) is None
    assert AuditEvent.objects.filter(action="api_token.revoke").count() == 1

    _, p3 = ApiToken.issue(user, "inactive")
    user.is_active = False
    user.save()
    assert ApiToken.authenticate(p3) is None


def test_default_ttl_comes_from_settings(user, settings):
    settings.API_TOKEN_DEFAULT_TTL_DAYS = 7
    token, _ = ApiToken.issue(user, "ci")
    assert timedelta(days=6) < token.expires_at - timezone.now() <= timedelta(days=7)


def test_whoami_with_bearer_token(client, user):
    UserRole.grant(user, Role.Code.ANALYST, by=None)
    _, plaintext = ApiToken.issue(user, "ci", scopes=["reporting:read"])
    resp = client.get(reverse("api:whoami"), HTTP_AUTHORIZATION=f"Bearer {plaintext}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"] == "ops@example.com"
    assert body["auth"] == "token"
    assert body["roles"] == ["ANALYST"]
    assert body["scopes"] == ["reporting:read"]


@pytest.mark.parametrize("header", [None, "Bearer ept_nope_nope", "Bearer a b"])
def test_whoami_rejects_missing_or_bad_tokens(client, header):
    extra = {"HTTP_AUTHORIZATION": header} if header else {}
    resp = client.get(reverse("api:whoami"), **extra)
    assert resp.status_code == 401
    assert resp["WWW-Authenticate"] == "Bearer"


# ─── Admin ───────────────────────────────────────────────────────────────


def test_every_admin_changelist_loads(login_verified, staff):
    client = login_verified(staff)
    for model in admin.site._registry:
        url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
        assert client.get(url).status_code == 200, url


@pytest.mark.parametrize("model", [AuditEvent, ConsentRecord, ApiToken])
def test_append_only_and_minted_rows_cannot_be_added_in_admin(login_verified, staff, model):
    client = login_verified(staff)
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_add")
    assert client.get(url).status_code == 403


def test_admin_erase_action_is_audited(login_verified, staff):
    c = Contact.objects.create(email="bob@example.com")
    client = login_verified(staff)
    client.post(
        reverse("admin:contacts_contact_changelist"),
        {"action": "erase", "_selected_action": [str(c.pk)]},
    )
    c.refresh_from_db()
    assert c.deleted_at is not None
    assert AuditEvent.objects.get(action="contact.erase").actor == staff


# ─── Seed script ─────────────────────────────────────────────────────────


def _load_seed():
    spec = importlib.util.spec_from_file_location("seed_dev", REPO_ROOT / "scripts" / "seed_dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_is_idempotent_and_only_uses_reserved_addresses(capsys):
    seed = _load_seed()
    seed.main()
    seed.main()
    assert Contact.objects.count() == seed.CONTACT_COUNT
    assert ConsentRecord.objects.count() == seed.CONTACT_COUNT
    assert not Contact.objects.exclude(email__endswith="@example.test").exists()
    assert "0 created" in capsys.readouterr().out


def test_seed_refuses_outside_local(settings):
    settings.ENVIRONMENT = "staging"
    with pytest.raises(SystemExit):
        _load_seed().main()
