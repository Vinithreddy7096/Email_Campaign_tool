"""
BUILD.md §4.2 boot guards.

These are safety interlocks, not validation niceties. The single most important
assertion in this file is that a production configuration with MOCK_MODE=true is
refused: MOCK_MODE silently redirects all mail to a sink, so a production
instance running in mock mode would accept campaigns and deliver nothing.
"""

import os

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

from config.settings import base


@pytest.fixture
def prod(monkeypatch, tmp_path):
    """A production config that passes every guard. Tests break one thing each."""
    keydir = tmp_path / "keys"
    keydir.mkdir(mode=0o700)
    monkeypatch.setattr(base, "MOCK_MODE", False)
    monkeypatch.setattr(base, "DEBUG", False)
    monkeypatch.setattr(base, "SECRET_KEY", "x" * 50)
    monkeypatch.setattr(base, "BASE_URL", "https://app.example.internal")
    monkeypatch.setattr(base, "MESSAGE_ID_DOMAIN", "mail.example.com")
    monkeypatch.setattr(base, "DEFAULT_RETURN_PATH_DOMAIN", "bounce.example.com")
    monkeypatch.setattr(base, "ABUSE_CONTACT_EMAIL", "abuse@example.com")
    monkeypatch.setattr(base, "WEBHOOK_SIGNING_SECRET", "s" * 32)
    monkeypatch.setattr(base, "DKIM_KEY_DIR", str(keydir))
    monkeypatch.setattr(base, "FIELD_ENCRYPTION_KEYS", [Fernet.generate_key().decode()])
    monkeypatch.setattr(base, "MFA_ENFORCED", True)
    monkeypatch.setattr(base, "MTA_INGEST_SECRET", "m" * 32)
    monkeypatch.setattr(base, "METRICS_TOKEN", "p" * 32)
    return monkeypatch


def test_the_fixture_config_boots(prod):
    base.run_boot_guards("production")


@pytest.mark.parametrize(("name", "value", "message"), [
    ("MTA_INGEST_SECRET", "", "MTA_INGEST_SECRET"),
    ("MTA_INGEST_SECRET", "s" * 32, "must differ"),  # same as WEBHOOK_SIGNING_SECRET
    ("METRICS_TOKEN", "short", "METRICS_TOKEN"),
])  # fmt: skip
def test_production_requires_intake_and_metrics_secrets(prod, name, value, message):
    prod.setattr(base, name, value)
    with pytest.raises(ImproperlyConfigured, match=message):
        base.run_boot_guards("production")


def test_production_refuses_mfa_off(prod):
    prod.setattr(base, "MFA_ENFORCED", False)
    with pytest.raises(ImproperlyConfigured, match="MFA_ENFORCED"):
        base.run_boot_guards("production")


@pytest.mark.parametrize("keys", [[], ["not-a-fernet-key"]])
def test_production_requires_valid_field_encryption_keys(prod, keys):
    """Q-SEC-1: MFA secrets must not be encrypted under a SECRET_KEY-derived key."""
    prod.setattr(base, "FIELD_ENCRYPTION_KEYS", keys)
    with pytest.raises(ImproperlyConfigured, match="FIELD_ENCRYPTION_KEYS"):
        base.run_boot_guards("production")


def test_valid_production_config_boots(prod):
    base.run_boot_guards("production")  # must not raise


def test_production_refuses_mock_mode(prod):
    prod.setattr(base, "MOCK_MODE", True)
    with pytest.raises(ImproperlyConfigured, match="MOCK_MODE must be false"):
        base.run_boot_guards("production")


def test_production_refuses_debug(prod):
    prod.setattr(base, "DEBUG", True)
    with pytest.raises(ImproperlyConfigured, match="DEBUG must be false"):
        base.run_boot_guards("production")


@pytest.mark.parametrize("key", ["", "short", "x" * 49])
def test_production_requires_strong_secret_key(prod, key):
    prod.setattr(base, "SECRET_KEY", key)
    with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
        base.run_boot_guards("production")


def test_production_requires_https_base_url(prod):
    prod.setattr(base, "BASE_URL", "http://app.example.internal")
    with pytest.raises(ImproperlyConfigured, match="BASE_URL must be https"):
        base.run_boot_guards("production")


@pytest.mark.parametrize(
    ("attr", "needle"),
    [
        ("MESSAGE_ID_DOMAIN", "MESSAGE_ID_DOMAIN"),
        ("DEFAULT_RETURN_PATH_DOMAIN", "DEFAULT_RETURN_PATH_DOMAIN"),
        ("ABUSE_CONTACT_EMAIL", "ABUSE_CONTACT_EMAIL"),
        ("WEBHOOK_SIGNING_SECRET", "WEBHOOK_SIGNING_SECRET"),
    ],
)
def test_production_requires_identity_and_abuse_config(prod, attr, needle):
    prod.setattr(base, attr, "")
    with pytest.raises(ImproperlyConfigured, match=needle):
        base.run_boot_guards("production")


def test_production_reports_every_problem_at_once(prod):
    """An operator should see the whole list, not fix-one-rerun-repeat."""
    prod.setattr(base, "MOCK_MODE", True)
    prod.setattr(base, "SECRET_KEY", "")
    prod.setattr(base, "BASE_URL", "http://nope")
    with pytest.raises(ImproperlyConfigured) as exc:
        base.run_boot_guards("production")
    message = str(exc.value)
    assert "MOCK_MODE" in message
    assert "SECRET_KEY" in message
    assert "BASE_URL" in message


def test_app_nodes_need_no_dkim_keys(prod):
    """OpenDKIM signs on the MTA hosts; app nodes should not hold private keys."""
    prod.setattr(base, "DKIM_KEY_DIR", "/nonexistent/opendkim/keys")
    base.run_boot_guards("production")


@pytest.mark.skipif(os.name != "posix", reason="mode bits are not meaningful on Windows")
def test_production_rejects_world_readable_dkim_keys(prod, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    prod.setattr(base, "DKIM_KEY_DIR", str(loose))
    with pytest.raises(ImproperlyConfigured, match="world-accessible"):
        base.run_boot_guards("production")


# ─── Non-production ──────────────────────────────────────────────────────


def test_nonprod_allows_mock_mode(monkeypatch):
    monkeypatch.setattr(base, "MOCK_MODE", True)
    monkeypatch.setattr(base, "ALLOWED_TEST_RECIPIENT_DOMAINS", [])
    base.run_boot_guards("staging")  # must not raise


def test_nonprod_allows_live_smtp_only_with_an_allowlist(monkeypatch):
    monkeypatch.setattr(base, "MOCK_MODE", False)
    monkeypatch.setattr(base, "ALLOWED_TEST_RECIPIENT_DOMAINS", ["example.test"])
    base.run_boot_guards("staging")  # must not raise


def test_nonprod_refuses_unrestricted_sending(monkeypatch):
    """Staging must never be able to mail arbitrary public recipients."""
    monkeypatch.setattr(base, "MOCK_MODE", False)
    monkeypatch.setattr(base, "ALLOWED_TEST_RECIPIENT_DOMAINS", [])
    with pytest.raises(ImproperlyConfigured, match="ALLOWED_TEST_RECIPIENT_DOMAINS"):
        base.run_boot_guards("staging")


def test_nonprod_does_not_require_production_identity_config(monkeypatch):
    monkeypatch.setattr(base, "MOCK_MODE", True)
    monkeypatch.setattr(base, "MESSAGE_ID_DOMAIN", "")
    monkeypatch.setattr(base, "WEBHOOK_SIGNING_SECRET", "")
    base.run_boot_guards("local")  # must not raise
