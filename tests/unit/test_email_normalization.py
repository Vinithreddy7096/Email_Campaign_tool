"""§8.1: contacts, suppression and consent must agree on what "the same address" is."""

import pytest

from common.email import InvalidEmailError, email_hash, normalize_domain, normalize_email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bob@example.com", "bob@example.com"),
        ("  Bob@Example.COM  ", "bob@example.com"),
        ("bob@example.com.", "bob@example.com"),
        ("user@bücher.example", "user@xn--bcher-kva.example"),
        ('"quoted@local"@example.com', '"quoted@local"@example.com'),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "no-at-sign", "@example.com", "bob@", "a b@example.com"])
def test_normalize_email_rejects_garbage(raw):
    with pytest.raises(InvalidEmailError):
        normalize_email(raw)


def test_hash_is_case_and_whitespace_insensitive():
    """A suppression on bob@x must match Bob@X — otherwise it can be mailed."""
    assert email_hash(" Bob@Example.com") == email_hash("bob@example.com")
    assert len(email_hash("bob@example.com")) == 64


def test_unicode_and_punycode_domains_hash_identically():
    assert email_hash("u@bücher.example") == email_hash("u@xn--bcher-kva.example")


def test_normalize_domain_rejects_invalid():
    with pytest.raises(InvalidEmailError):
        normalize_domain("bad_domain!.com")


def test_overlong_address_rejected():
    with pytest.raises(InvalidEmailError):
        normalize_email("a" * 250 + "@example.com")
