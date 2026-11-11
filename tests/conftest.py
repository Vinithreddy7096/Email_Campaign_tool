import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import pytest  # noqa: E402


@pytest.fixture
def login_verified(client):
    """Log ``user`` in with MFA already satisfied for this session."""

    def _login(user):
        from accounts.middleware import SESSION_KEY

        client.force_login(user)
        session = client.session
        session[SESSION_KEY] = str(user.pk)
        session.save()
        return client

    return _login


@pytest.fixture
def dns_zone():
    """A fresh, mutable copy of tests/fixtures/dns_responses.yml."""
    from pathlib import Path

    import yaml

    path = Path(__file__).parent / "fixtures" / "dns_responses.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def resolver(dns_zone):
    """Resolver over ``dns_zone``. Never touches the network."""
    from workers.services.dns_checks import DnsLookupError

    def resolve(name, rdtype):
        answer = dns_zone.get(name.rstrip(".").lower(), {}).get(rdtype, [])
        if answer == "SERVFAIL":
            raise DnsLookupError(f"{rdtype} {name}: SERVFAIL")
        return list(answer)

    return resolve
