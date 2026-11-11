"""
Custom model fields.

Key management: EncryptedCharField uses FIELD_ENCRYPTION_KEYS, a list of Fernet
keys independent of SECRET_KEY. The first key encrypts and every key decrypts,
so keys rotate by prepending a new one. Outside production an empty list falls
back to a key derived from SECRET_KEY for convenience; production refuses to
boot without real keys (Q-SEC-1). Where the keys live (Vault, cloud secret
manager, Ansible Vault) is still undecided.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> MultiFernet:
    keys = list(getattr(settings, "FIELD_ENCRYPTION_KEYS", []))
    if keys:
        return MultiFernet([Fernet(k) for k in keys])
    if not settings.SECRET_KEY:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS or SECRET_KEY is required")
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return MultiFernet([Fernet(base64.urlsafe_b64encode(digest))])


class EncryptedCharField(models.TextField):
    """Transparently encrypts at rest. Stored as a Fernet token (text).

    The column is TextField because ciphertext is longer than the plaintext and
    has no useful max_length. Values are not indexable or searchable — that is
    intentional for secrets such as MFA seeds.
    """

    def get_prep_value(self, value: Any) -> str | None:
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str | None:
        if value is None or value == "":
            return value
        try:
            return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt field value: no key in FIELD_ENCRYPTION_KEYS matches. "
                "Was a key removed before data was re-encrypted? See Q-SEC-1."
            ) from exc
