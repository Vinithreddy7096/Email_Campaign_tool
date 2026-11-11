"""
TOTP (RFC 6238) for MFA — SHA-1, 6 digits, 30-second steps.

Those are the parameters every authenticator app (Google Authenticator, 1Password,
Authy, Aegis…) assumes when given a bare otpauth:// URI. Implemented on the stdlib
rather than pulling in a dependency; tests check it against the RFC's vectors.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlencode

STEP_SECONDS = 30
DIGITS = 6
ISSUER = "Email Platform"


def generate_secret() -> str:
    """160-bit secret, base32 — the RFC 4226 recommended length."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10**digits:0{digits}d}"


def time_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def matching_step(
    secret: str, code: str, *, now: float | None = None, window: int = 1
) -> int | None:
    """Return the time step ``code`` is valid for, or None.

    ``window`` steps either side tolerate clock drift. Callers must reject a step
    at or before the last one accepted, or a code could be replayed.
    """
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    key = base64.b32decode(secret)
    current = time_step(now)
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str) -> str:
    """otpauth:// URI for authenticator apps (Key Uri Format)."""
    label = quote(f"{ISSUER}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"
