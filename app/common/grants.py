"""Database privileges for the runtime role (docs/OPEN_QUESTIONS.md Q-DATA-1).

Postgres triggers (``common.db.forbid``) stop UPDATE/DELETE on append-only and
never-deleted tables, but not TRUNCATE (Django's test flush needs it), and a
table's *owner* can simply drop the triggers. So production uses two roles:

- an **owner** role that runs ``migrate`` and owns every table;
- an **app** role that the web and worker processes connect as. It owns
  nothing and gets exactly the privileges below — never TRUNCATE, never
  UPDATE/DELETE where the data model forbids them, never CREATE.

The privileges are derived, not hand-listed, so a new model is covered on the
next ``manage.py db_grants --apply``:

    AppendOnlyModel subclass                 SELECT, INSERT
    NoDeleteModel subclass                   SELECT, INSERT, UPDATE
    any other model table                    SELECT, INSERT, UPDATE, DELETE
    django_migrations                        SELECT
    minus whatever a ``*_forbid_update`` / ``*_forbid_delete`` trigger blocks

``problems()`` reports how the *current* connection's role falls short; the
``common.W010`` deploy check uses it, so ``check --deploy`` fails on a
production that still connects as the owner.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core import checks
from django.db import connection

from common.models import AppendOnlyModel, NoDeleteModel

ALL = ("SELECT", "INSERT", "UPDATE", "DELETE")
_TRIGGER_SQL = """
SELECT c.relname, t.tgname
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND NOT t.tgisinternal
"""
_TABLES_SQL = """
SELECT c.relname, pg_get_userbyid(c.relowner)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p')
"""


def _quote(name: str) -> str:
    return connection.ops.quote_name(name)


def _forbidden_by_triggers() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    with connection.cursor() as cur:
        cur.execute(_TRIGGER_SQL)
        for table, trigger in cur.fetchall():
            for op in ("update", "delete"):
                if trigger == f"{table}_forbid_{op}":
                    out.setdefault(table, set()).add(op.upper())
    return out


def policy() -> dict[str, tuple[str, ...]]:
    """Table → privileges the app role should hold (only tables that exist)."""
    with connection.cursor() as cur:
        cur.execute(_TABLES_SQL)
        existing = {name for name, _ in cur.fetchall()}
    forbidden = _forbidden_by_triggers()
    out: dict[str, tuple[str, ...]] = {}
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        if not meta.managed or meta.proxy or meta.db_table not in existing:
            continue
        if issubclass(model, AppendOnlyModel):
            privs = {"SELECT", "INSERT"}
        elif issubclass(model, NoDeleteModel):
            privs = {"SELECT", "INSERT", "UPDATE"}
        else:
            privs = set(ALL)
        privs -= forbidden.get(meta.db_table, set())
        out[meta.db_table] = tuple(p for p in ALL if p in privs)
    if "django_migrations" in existing:
        out["django_migrations"] = ("SELECT",)
    return dict(sorted(out.items()))


def protected_tables() -> set[str]:
    """Tables where the data model forbids UPDATE or DELETE."""
    return {t for t, privs in policy().items() if set(privs) != set(ALL)}


def grant_statements(app_role: str) -> list[str]:
    """SQL that gives ``app_role`` exactly the policy. Run as the owner role."""
    role = _quote(app_role)
    with connection.cursor() as cur:
        cur.execute(_TABLES_SQL)
        every_table = sorted(name for name, _ in cur.fetchall())
    stmts = [
        f"REVOKE CREATE ON SCHEMA {_quote(_schema())} FROM {role}",
        f"GRANT USAGE ON SCHEMA {_quote(_schema())} TO {role}",
    ]
    # Start from nothing on every table (including ones with no model), then
    # add exactly what the policy allows.
    stmts += [f"REVOKE ALL ON TABLE {_quote(t)} FROM {role}" for t in every_table]
    stmts += [
        f"GRANT {', '.join(privs)} ON TABLE {_quote(t)} TO {role}" for t, privs in policy().items()
    ]
    # nextval() for integer primary keys (email_templates, django.contrib.*).
    stmts.append(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {_quote(_schema())} TO {role}")
    return stmts


def _schema() -> str:
    with connection.cursor() as cur:
        cur.execute("SELECT current_schema()")
        return str(cur.fetchone()[0])


def problems() -> list[str]:
    """Ways the current connection's role has more power than the policy."""
    out: list[str] = []
    with connection.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        user, is_super = cur.fetchone()
        if is_super:
            return [f"connects as superuser {user!r}; triggers and grants mean nothing to it"]
        cur.execute(_TABLES_SQL)
        owners = dict(cur.fetchall())
        want = policy()
        for table in sorted(protected_tables()):
            if owners.get(table) == user:
                out.append(f"{user!r} owns {table} and could drop its protection triggers")
            allowed = set(want[table])
            for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                if priv in allowed:
                    continue
                cur.execute("SELECT has_table_privilege(current_user, %s, %s)", [table, priv])
                if cur.fetchone()[0]:
                    out.append(f"{user!r} holds {priv} on {table}")
    return out


def deploy_check(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    """common.W010 (``check --deploy``): the runtime role must not be able to
    bypass the append-only rules. Registered by CommonConfig.ready()."""
    try:
        found = problems()
    except Exception as exc:  # noqa: BLE001 — a deploy check must not crash
        found = [f"could not inspect database privileges: {exc.__class__.__name__}: {exc}"]
    return [
        checks.Warning(
            f"Database role too powerful: {p}",
            hint="Connect the app as a role that owns nothing, and grant it access with "
            "`manage.py db_grants --apply --app-role <role>` run as the owner. "
            "See docs/DEPLOYMENT.md, 'Database roles'.",
            id="common.W010",
        )
        for p in found
    ]
