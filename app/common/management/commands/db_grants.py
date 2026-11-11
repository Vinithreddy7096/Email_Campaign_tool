"""Give the app's runtime database role exactly the privileges it needs.

Run as the **owner** role after every ``migrate`` (the production migrate
service does this). See ``common.grants`` for the rules.

    manage.py db_grants --app-role mailer_app            # print the SQL
    manage.py db_grants --app-role mailer_app --apply    # run it
    manage.py db_grants --check                          # inspect the current role
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction

from common import grants


class Command(BaseCommand):
    help = "Grant the runtime DB role least privilege (no TRUNCATE, no UPDATE/DELETE on append-only tables)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--app-role", help="The role web and worker processes connect as.")
        parser.add_argument("--apply", action="store_true", help="Run the SQL instead of printing it.")
        parser.add_argument("--check", action="store_true",
                            help="Report how the role in DATABASE_URL exceeds the policy; exit 1 if it does.")  # fmt: skip

    def handle(self, *args: Any, **opts: Any) -> None:
        if opts["check"]:
            found = grants.problems()
            for p in found:
                self.stdout.write(f"PROBLEM: {p}")
            if found:
                raise CommandError(f"{len(found)} privilege problem(s)")
            self.stdout.write("OK: the current role cannot bypass the append-only rules.")
            return
        role = opts["app_role"]
        if not role:
            raise CommandError("--app-role is required (or use --check)")
        with connection.cursor() as cur:
            cur.execute("SELECT current_user, EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
                        [role])  # fmt: skip
            me, exists = cur.fetchone()
        if not exists:
            raise CommandError(f"role {role!r} does not exist; create it first (docs/DEPLOYMENT.md)")
        if role == me:
            raise CommandError(
                f"{role!r} is the role running this command, i.e. the table owner. "
                "The app must connect as a different role."
            )
        stmts = grants.grant_statements(role)
        if not opts["apply"]:
            for stmt in stmts:
                self.stdout.write(f"{stmt};")
            return
        with transaction.atomic(), connection.cursor() as cur:
            for stmt in stmts:
                cur.execute(stmt)
        self.stdout.write(f"Applied {len(stmts)} statements for role {role!r}.")
