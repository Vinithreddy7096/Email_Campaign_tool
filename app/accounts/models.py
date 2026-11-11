"""
Users, roles, API tokens (BUILD.md §6.1).

``User`` predates the rest: AUTH_USER_MODEL must be correct before the first
migration is generated. Role, UserRole and ApiToken are PROVISIONAL — written
without the §6.1 field list; see docs/OPEN_QUESTIONS.md Q-DATA-1.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from audit.models import AuditEvent
from common.fields import EncryptedCharField
from common.models import TimestampedModel, UUIDModel
from common.uuid7 import uuid7

from . import totp

MFA_MAX_FAILURES = 5
MFA_LOCKOUT = timedelta(minutes=15)


class UserManager(DjangoUserManager):
    """Email is the username field, so both create paths must require it."""

    def create_user(self, email: str, password: str | None = None, **extra):  # type: ignore[override]
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, username=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra):  # type: ignore[override]
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra.get("is_staff") is not True or extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True")
        return self.create_user(email, password, **extra)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(_("email address"), unique=True)

    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = EncryptedCharField(null=True, blank=True)
    # Highest TOTP step accepted so far. A code for this step or earlier is a
    # replay and is refused, even inside its 30-second window.
    mfa_last_used_step = models.BigIntegerField(null=True, blank=True, editable=False)

    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    failed_login_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    # ClassVar, not an instance annotation — AbstractBaseUser declares it as one.
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = UserManager()  # type: ignore[misc]

    class Meta:
        db_table = "accounts_user"
        indexes = [models.Index(fields=["email"])]

    def __str__(self) -> str:
        return self.email

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()

    @property
    def role_codes(self) -> set[str]:
        return set(self.user_roles.values_list("role__code", flat=True))

    def has_role(self, code: str) -> bool:
        return self.is_superuser or code in self.role_codes

    @property
    def mfa_required(self) -> bool:
        """MFA is mandatory for any role in MFA_REQUIRED_ROLES, and for superusers,
        who hold every permission. Enforced by MfaEnforcementMiddleware."""
        return self.is_superuser or bool(self.role_codes & set(settings.MFA_REQUIRED_ROLES))

    def enroll_mfa(self, secret: str, code: str) -> bool:
        """Turn MFA on, but only once the user proves their app produces valid codes."""
        step = totp.matching_step(secret, code)
        if step is None:
            return False
        with transaction.atomic():
            self.mfa_secret = secret
            self.mfa_enabled = True
            self.mfa_last_used_step = step
            self.failed_login_count = 0
            self.save(
                update_fields=[
                    "mfa_secret",
                    "mfa_enabled",
                    "mfa_last_used_step",
                    "failed_login_count",
                    "updated_at",
                ]
            )
            AuditEvent.record("mfa.enroll", actor=self, target=self)
        return True

    def verify_mfa(self, code: str) -> bool:
        """Check a TOTP code. Refuses replays; locks the account after
        MFA_MAX_FAILURES consecutive bad codes."""
        with transaction.atomic():
            # Row lock: two concurrent requests must not both accept one code.
            user = type(self).objects.select_for_update().get(pk=self.pk)
            if not user.mfa_enabled or not user.mfa_secret or user.is_locked:
                return False
            if user.locked_until is not None:  # an expired lock: start counting afresh
                user.locked_until = None
                user.failed_login_count = 0
            step = totp.matching_step(user.mfa_secret, code)
            replay = step is not None and (
                user.mfa_last_used_step is not None and step <= user.mfa_last_used_step
            )
            if step is None or replay:
                user.failed_login_count += 1
                fields = ["failed_login_count", "locked_until", "updated_at"]
                AuditEvent.record("mfa.verify_failed", actor=user, target=user, replay=replay)
                if user.failed_login_count >= MFA_MAX_FAILURES:
                    user.locked_until = timezone.now() + MFA_LOCKOUT
                    AuditEvent.record("mfa.lockout", actor=user, target=user)
                user.save(update_fields=fields)
                self.failed_login_count, self.locked_until = (
                    user.failed_login_count,
                    user.locked_until,
                )
                return False
            user.mfa_last_used_step = step
            user.failed_login_count = 0
            user.save(
                update_fields=[
                    "mfa_last_used_step",
                    "failed_login_count",
                    "locked_until",
                    "updated_at",
                ]
            )
            self.mfa_last_used_step, self.failed_login_count = step, 0
            return True

    def reset_mfa(self, *, by: User | None) -> None:
        """Lost device. The user must enrol again at next login."""
        with transaction.atomic():
            self.mfa_secret = None
            self.mfa_enabled = False
            self.mfa_last_used_step = None
            self.failed_login_count = 0
            self.locked_until = None
            self.save()
            AuditEvent.record("mfa.reset", actor=by, target=self)


class Role(TimestampedModel):
    class Code(models.TextChoices):
        SYSTEM_ADMIN = "SYSTEM_ADMIN", "System administrator"
        APPROVER = "APPROVER", "Campaign approver"
        CAMPAIGN_MANAGER = "CAMPAIGN_MANAGER", "Campaign manager"
        CONTENT_EDITOR = "CONTENT_EDITOR", "Content editor"
        ANALYST = "ANALYST", "Analyst (read-only)"

    code = models.CharField(max_length=32, choices=Code.choices, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "accounts_role"

    def __str__(self) -> str:
        return self.get_code_display()


class UserRole(UUIDModel):
    """A grant of one role to one user. Grants and revocations are audited."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="grants")
    granted_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "accounts_user_role"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="accounts_user_role_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.user} → {self.role}"

    @classmethod
    def grant(cls, user: User, code: str, *, by: User | None) -> tuple[UserRole, bool]:
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                user=user, role=Role.objects.get(code=code), defaults={"granted_by": by}
            )
            if created:
                AuditEvent.record("role.grant", actor=by, target=user, role=code)
            user.__dict__.pop("_role_perm_cache", None)  # see accounts.backends
            return row, created

    @classmethod
    def revoke(cls, user: User, code: str, *, by: User | None) -> bool:
        with transaction.atomic():
            deleted, _ = cls.objects.filter(user=user, role__code=code).delete()
            if deleted:
                AuditEvent.record("role.revoke", actor=by, target=user, role=code)
            user.__dict__.pop("_role_perm_cache", None)  # see accounts.backends
            return bool(deleted)


TOKEN_TAG = "ept"  # noqa: S105 — public token-format prefix, not a secret


def _hash_secret(secret: str) -> str:
    # The secret is 256 bits of randomness, so a fast hash is sufficient; a slow
    # KDF only matters for low-entropy, human-chosen passwords.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ApiToken(TimestampedModel):
    """Bearer token for API clients. Format: ``ept_<prefix>_<secret>``.

    Only the SHA-256 of the secret is stored; the plaintext is returned once, by
    ``issue()``. ``prefix`` is a non-secret lookup key so verification is a
    single indexed read plus a constant-time compare. Scope names are not yet
    specified (Q-DATA-1); an empty scope list grants nothing.
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="api_tokens")
    name = models.CharField(max_length=128)
    prefix = models.CharField(max_length=16, unique=True, editable=False)
    secret_hash = models.CharField(max_length=64, editable=False)
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    class Meta:
        db_table = "accounts_api_token"

    def __str__(self) -> str:
        return f"{self.name} ({TOKEN_TAG}_{self.prefix}_…)"

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now() and self.user.is_active

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    @classmethod
    def issue(
        cls,
        user: User,
        name: str,
        *,
        scopes: list[str] | None = None,
        ttl_days: int | None = None,
        by: User | None = None,
    ) -> tuple[ApiToken, str]:
        """Create a token. The plaintext is returned here and never stored."""
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        days = ttl_days if ttl_days is not None else settings.API_TOKEN_DEFAULT_TTL_DAYS
        with transaction.atomic():
            token = cls.objects.create(
                user=user,
                name=name,
                prefix=prefix,
                secret_hash=_hash_secret(secret),
                scopes=list(scopes or []),
                expires_at=timezone.now() + timedelta(days=days),
                created_by=by,
            )
            AuditEvent.record(
                "api_token.issue", actor=by, target=token, user=str(user.pk), scopes=token.scopes
            )
        return token, f"{TOKEN_TAG}_{prefix}_{secret}"

    @classmethod
    def authenticate(cls, raw: str) -> ApiToken | None:
        """Return the usable token for ``raw``, or None. Never says which check failed."""
        tag, _, rest = raw.partition("_")
        prefix, _, secret = rest.partition("_")
        if tag != TOKEN_TAG or not prefix or not secret:
            return None
        token = cls.objects.select_related("user").filter(prefix=prefix).first()
        if token is None or not hmac.compare_digest(token.secret_hash, _hash_secret(secret)):
            return None
        if not token.is_usable:
            return None
        cls.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        return token

    def revoke(self, *, by: User | None) -> None:
        if self.revoked_at is not None:
            return
        with transaction.atomic():
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at", "updated_at"])
            AuditEvent.record("api_token.revoke", actor=by, target=self)
