"""Mint an API token. The plaintext is printed once and never stored."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts.models import ApiToken, User


class Command(BaseCommand):
    help = "Issue an API token for a user. The secret is shown once."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument("--name", required=True, help="What the token is for")
        parser.add_argument("--scope", action="append", default=[], dest="scopes")
        parser.add_argument("--ttl-days", type=int, default=None)

    def handle(self, *args: Any, **opts: Any) -> None:
        try:
            user = User.objects.get(email__iexact=opts["email"], is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError(f"no active user {opts['email']!r}") from exc
        token, plaintext = ApiToken.issue(
            user, opts["name"], scopes=opts["scopes"], ttl_days=opts["ttl_days"]
        )
        self.stdout.write(
            f"Issued '{token.name}' for {user.email}, expires {token.expires_at:%Y-%m-%d}."
        )
        self.stdout.write("Store it now — it cannot be shown again:\n")
        self.stdout.write(plaintext)
