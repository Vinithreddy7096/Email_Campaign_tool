"""TOTP must interoperate with real authenticator apps, so it is checked against
the published RFC test vectors rather than against itself."""

import base64
from urllib.parse import parse_qs, urlparse

import pytest

from accounts import totp

RFC_KEY = b"12345678901234567890"
RFC_SECRET = base64.b32encode(RFC_KEY).decode()


def test_hotp_matches_rfc4226_appendix_d():
    expected = ["755224", "287082", "359152", "969429", "338314",
                "254676", "287922", "162583", "399871", "520489"]  # fmt: skip
    assert [totp.hotp(RFC_KEY, i) for i in range(10)] == expected


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_rfc6238_appendix_b_sha1(unix_time, code):
    assert totp.hotp(RFC_KEY, totp.time_step(unix_time), digits=8) == code


def _code_at(t):
    return totp.hotp(RFC_KEY, totp.time_step(t))


def test_matching_step_accepts_current_and_adjacent_steps_only():
    now = 1_700_000_000
    step = totp.time_step(now)
    assert totp.matching_step(RFC_SECRET, _code_at(now), now=now) == step
    assert totp.matching_step(RFC_SECRET, _code_at(now - 30), now=now) == step - 1
    assert totp.matching_step(RFC_SECRET, _code_at(now + 30), now=now) == step + 1
    assert totp.matching_step(RFC_SECRET, _code_at(now - 60), now=now) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 5"])
def test_malformed_codes_are_rejected(bad):
    assert totp.matching_step(RFC_SECRET, bad, now=1_700_000_000) is None


def test_spaces_in_codes_are_tolerated():
    now = 1_700_000_000
    code = _code_at(now)
    assert totp.matching_step(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now) is not None


def test_generated_secret_is_160_bits_of_base32():
    assert len(base64.b32decode(totp.generate_secret())) == 20


def test_provisioning_uri_is_well_formed():
    uri = urlparse(totp.provisioning_uri("JBSWY3DPEHPK3PXP", "ops@example.com"))
    assert (uri.scheme, uri.netloc) == ("otpauth", "totp")
    assert uri.path == "/Email%20Platform%3Aops%40example.com"
    q = parse_qs(uri.query)
    assert q["secret"] == ["JBSWY3DPEHPK3PXP"] and q["issuer"] == ["Email Platform"]
