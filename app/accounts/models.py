"""
Users (BUILD.md §6.1).

Scope note: only ``User`` is defined in the foundation phase, because
AUTH_USER_MODEL must be correct before the first migration is ever generated —
changing it afterwards is a rebuild, not a migration. Role, UserRole and
ApiToken belong to the identity-and-access phase and are deliberately absent.
"""

from __future__ import annotations

from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.fields import EncryptedCharField
from common.uuid7 import uuid7


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
        from django.utils import timezone

        return self.locked_until is not None and self.locked_until > timezone.now()
