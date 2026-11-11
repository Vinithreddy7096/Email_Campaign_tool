"""Lost-device recovery: clear a user's MFA so they enrol again at next login."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts.models import User


class Command(BaseCommand):
    help = "Reset a user's MFA (lost device). Audited. They must re-enrol at next login."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")

    def handle(self, *args: Any, **opts: Any) -> None:
        try:
            user = User.objects.get(email__iexact=opts["email"])
        except User.DoesNotExist as exc:
            raise CommandError(f"no user {opts['email']!r}") from exc
        user.reset_mfa(by=None)
        self.stdout.write(f"MFA reset for {user.email}. They will enrol again at next login.")
