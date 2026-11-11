"""
Email address normalization and hashing (§8.1).

Every table that matches on an address (contacts, suppression, consent) stores
the output of ``normalize_email`` and ``email_hash``. They must agree, or a
suppressed address could be matched as a different one and mailed.

PROVISIONAL — the §8.1 rules were not available when this was written; see
docs/OPEN_QUESTIONS.md Q-DATA-1. Two choices here are policy, not syntax:

- the local part is lowercased. RFC 5321 says it is case-sensitive, but no
  major mailbox provider treats it that way, and a case-sensitive match would
  let ``Bob@x`` slip past a suppression on ``bob@x``;
- the hash is unkeyed SHA-256. It is a stable match key, not anonymization —
  a known address can be re-identified by hashing it.
"""

from __future__ import annotations

import hashlib

import idna

__all__ = ["InvalidEmailError", "email_hash", "normalize_domain", "normalize_email"]

MAX_EMAIL_LENGTH = 254


class InvalidEmailError(ValueError):
    pass


def normalize_domain(raw: str) -> str:
    """Lowercase and punycode-encode a domain (UTS #46)."""
    value = raw.strip().rstrip(".")
    if not value:
        raise InvalidEmailError("empty domain")
    try:
        return idna.encode(value, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise InvalidEmailError(f"invalid domain {raw!r}: {exc}") from exc


def normalize_email(raw: str) -> str:
    value = raw.strip()
    local, sep, domain = value.rpartition("@")
    if not sep or not local or not domain:
        raise InvalidEmailError(f"not an email address: {raw!r}")
    if any(ch.isspace() for ch in local):
        raise InvalidEmailError(f"whitespace in local part: {raw!r}")
    normalized = f"{local.lower()}@{normalize_domain(domain)}"
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError(f"address longer than {MAX_EMAIL_LENGTH} characters")
    return normalized


def email_hash(raw: str) -> str:
    """SHA-256 hex of the normalized address. Accepts raw or normalized input."""
    return hashlib.sha256(normalize_email(raw).encode("utf-8")).hexdigest()
