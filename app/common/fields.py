"""
Custom model fields.

⚠️ Key management: EncryptedCharField derives its key from SECRET_KEY. Rotating
SECRET_KEY therefore makes existing ciphertext undecryptable. A dedicated,
separately-rotatable field-encryption key belongs in Vault — see
docs/OPEN_QUESTIONS.md (Q-SEC-1) before this is used in production.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet() -> Fernet:
    if not settings.SECRET_KEY:
        raise ImproperlyConfigured("SECRET_KEY is required to derive the field-encryption key")
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


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
                "Could not decrypt field value. SECRET_KEY may have been rotated; "
                "see docs/OPEN_QUESTIONS.md Q-SEC-1."
            ) from exc
