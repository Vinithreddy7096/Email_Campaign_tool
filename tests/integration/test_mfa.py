"""MFA enforcement: who is challenged, and what a valid, bad or replayed code does."""

import base64
import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts import totp
from accounts.middleware import SESSION_KEY
from accounts.models import MFA_MAX_FAILURES, Role, UserRole
from audit.models import AuditEvent

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def mfa_on(settings):
    """These tests are about enforcement; a local .env may switch it off."""
    settings.MFA_ENFORCED = True


SETUP = reverse("accounts:mfa-setup")
VERIFY = reverse("accounts:mfa-verify")


@pytest.fixture
def plain_user(django_user_model):
    return django_user_model.objects.create_user("analyst@example.com", "pw", is_staff=True)


@pytest.fixture
def approver(plain_user):
    UserRole.grant(plain_user, Role.Code.APPROVER, by=None)
    return plain_user


def code_for(secret, offset_steps=0):
    return totp.hotp(base64.b32decode(secret), totp.time_step() + offset_steps)


def enrolled(user):
    secret = totp.generate_secret()
    assert user.enroll_mfa(secret, code_for(secret))
    # Move past the enrolment step so a fresh code is not a replay.
    user.mfa_last_used_step -= 1
    user.save(update_fields=["mfa_last_used_step"])
    return secret


# ─── Who is challenged ───────────────────────────────────────────────────


def test_user_without_mfa_role_is_not_challenged(client, plain_user):
    client.force_login(plain_user)
    assert client.get("/admin/").status_code == 200


def test_required_role_is_sent_to_setup(client, approver):
    client.force_login(approver)
    resp = client.get("/admin/")
    assert resp.status_code == 302 and resp["Location"].startswith(SETUP)
    assert "next=%2Fadmin%2F" in resp["Location"]


def test_superuser_is_always_challenged(client, django_user_model):
    root = django_user_model.objects.create_superuser("root@example.com", "pw")
    client.force_login(root)
    assert client.get("/admin/")["Location"].startswith(SETUP)


def test_enrolled_user_is_sent_to_verify_even_without_required_role(client, plain_user):
    enrolled(plain_user)
    client.force_login(plain_user)
    assert client.get("/admin/")["Location"].startswith(VERIFY)


def test_session_api_calls_get_403_not_a_redirect(client, approver):
    client.force_login(approver)
    resp = client.get(reverse("api:whoami"))
    assert resp.status_code == 403 and "MFA" in resp.json()["detail"]


def test_exempt_paths_stay_reachable(client, approver):
    client.force_login(approver)
    assert client.get("/health/live").status_code == 200
    assert client.get(SETUP).status_code == 200


def test_logging_in_again_requires_mfa_again(client, approver):
    enrolled(approver)
    client.force_login(approver)
    session = client.session
    session[SESSION_KEY] = str(approver.pk)
    session.save()
    assert client.get("/admin/").status_code == 200
    client.force_login(approver)  # fires user_logged_in
    assert client.get("/admin/")["Location"].startswith(VERIFY)


# ─── Enrolment ───────────────────────────────────────────────────────────


def _pending_secret(resp):
    return (
        re.search(r"data-mfa-secret>([A-Z2-7 ]+)<", resp.content.decode()).group(1).replace(" ", "")
    )


def test_enrolment_flow(client, approver):
    client.force_login(approver)
    page = client.get(SETUP, {"next": "/admin/"})
    secret = _pending_secret(page)
    assert "otpauth://totp/" in page.content.decode()

    approver.refresh_from_db()
    assert not approver.mfa_enabled and approver.mfa_secret is None  # not saved until proven

    bad = client.post(SETUP, {"code": "000000", "next": "/admin/"})
    assert bad.status_code == 400

    ok = client.post(SETUP, {"code": code_for(secret), "next": "/admin/"})
    assert ok.status_code == 302 and ok["Location"] == "/admin/"
    approver.refresh_from_db()
    assert approver.mfa_enabled and approver.mfa_secret == secret
    assert client.get("/admin/").status_code == 200
    assert AuditEvent.objects.filter(action="mfa.enroll", actor=approver).exists()


def test_mfa_secret_is_encrypted_at_rest(approver):
    from django.db import connection

    secret = enrolled(approver)
    with connection.cursor() as cur:
        cur.execute("SELECT mfa_secret FROM accounts_user WHERE id = %s", [approver.pk])
        stored = cur.fetchone()[0]
    assert secret not in stored


def test_open_redirect_via_next_is_refused(client, approver):
    client.force_login(approver)
    secret = _pending_secret(client.get(SETUP))
    resp = client.post(SETUP, {"code": code_for(secret), "next": "https://evil.example/"})
    assert resp["Location"] == "/"


# ─── Verification ────────────────────────────────────────────────────────


def test_verify_flow(client, approver):
    secret = enrolled(approver)
    client.force_login(approver)
    assert client.post(VERIFY, {"code": "000000"}).status_code == 400
    resp = client.post(VERIFY, {"code": code_for(secret), "next": "/admin/"})
    assert resp.status_code == 302 and resp["Location"] == "/admin/"
    assert client.get("/admin/").status_code == 200


def test_a_code_cannot_be_replayed(approver):
    secret = enrolled(approver)
    code = code_for(secret)
    assert approver.verify_mfa(code)
    assert not approver.verify_mfa(code)
    assert AuditEvent.objects.filter(action="mfa.verify_failed").get().metadata["replay"]


def test_lockout_after_repeated_failures(approver):
    secret = enrolled(approver)
    for _ in range(MFA_MAX_FAILURES):
        assert not approver.verify_mfa("000000")
    approver.refresh_from_db()
    assert approver.is_locked
    # Even the right code is refused while locked.
    assert not approver.verify_mfa(code_for(secret))
    assert AuditEvent.objects.filter(action="mfa.lockout").exists()


def test_expired_lockout_starts_a_fresh_count(approver):
    enrolled(approver)
    for _ in range(MFA_MAX_FAILURES):
        approver.verify_mfa("000000")
    approver.locked_until = timezone.now() - timedelta(seconds=1)
    approver.save(update_fields=["locked_until"])
    # One typo after the lock expires must not re-lock immediately.
    assert not approver.verify_mfa("000000")
    approver.refresh_from_db()
    assert not approver.is_locked and approver.failed_login_count == 1


def test_reset_clears_mfa_and_is_audited(approver, django_user_model):
    enrolled(approver)
    admin_user = django_user_model.objects.create_superuser("root@example.com", "pw")
    approver.reset_mfa(by=admin_user)
    approver.refresh_from_db()
    assert not approver.mfa_enabled and approver.mfa_secret is None
    assert AuditEvent.objects.get(action="mfa.reset").actor == admin_user
