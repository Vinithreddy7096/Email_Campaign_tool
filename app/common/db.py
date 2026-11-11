"""
Migration helpers that enforce row immutability inside Postgres.

Model-level guards (``common.models``) only cover the ORM. These triggers also
stop raw SQL, ``manage.py dbshell`` and any future code path. They fire for the
table owner too; only ``session_replication_role = replica`` skips them.

TRUNCATE is deliberately not trigger-blocked: Django's ``flush`` and every
transactional test reset tables with it. The app's database role must instead
lack the TRUNCATE privilege on append-only tables — docs/OPEN_QUESTIONS.md
Q-DATA-1. Triggers stop mistakes, not the table owner, who can drop them.
"""

from __future__ import annotations

from django.db import migrations

_FUNCTION = "common_forbid_mutation"

_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% on % is forbidden by the data model', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""

_ALLOWED_OPS = {"UPDATE", "DELETE"}


def forbid(table: str, *ops: str) -> migrations.RunSQL:
    """Block the given operations on ``table`` with BEFORE triggers."""
    unknown = set(ops) - _ALLOWED_OPS
    if unknown or not ops:
        raise ValueError(f"ops must be a non-empty subset of {_ALLOWED_OPS}, got {ops}")

    forward = [_CREATE_FUNCTION]
    reverse = []
    for op in ops:
        name = f"{table}_forbid_{op.lower()}"
        forward.append(
            f"CREATE TRIGGER {name} BEFORE {op} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}();"
        )
        reverse.append(f"DROP TRIGGER IF EXISTS {name} ON {table};")
    return migrations.RunSQL(tuple(forward), tuple(reverse))
