"""Least-privilege runtime DB role (common.grants, manage.py db_grants).

Each test creates a throwaway role inside the test transaction (CREATE ROLE is
transactional in Postgres), grants it the policy, switches to it with SET
LOCAL ROLE and checks what Postgres itself allows."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import DatabaseError, connection, transaction

from common import grants

pytestmark = [pytest.mark.integration, pytest.mark.django_db]
ROLE = "ep_test_app_role"


@pytest.fixture
def app_role():
    with connection.cursor() as cur:
        cur.execute(f"CREATE ROLE {ROLE} NOLOGIN")
        for stmt in grants.grant_statements(ROLE):
            cur.execute(stmt)
    return ROLE


def as_role(sql, params=None):
    """Run ``sql`` as the app role; return rows, or the error text.

    A failed statement rolls the savepoint back (and SET LOCAL with it); a
    successful one does not, so the role is reset explicitly."""
    try:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(f"SET LOCAL ROLE {ROLE}")
            try:
                cur.execute(sql, params)
                return cur.fetchall() if cur.description else "ok"
            finally:
                cur.execute("RESET ROLE")
    except DatabaseError as exc:
        return str(exc).splitlines()[0]


def test_policy_matches_every_protection_trigger():
    policy = grants.policy()
    for table, ops in grants._forbidden_by_triggers().items():
        assert not set(ops) & set(policy[table]), f"{table} grants an op its trigger forbids"
    # Spot checks across apps, including tables added in this change.
    assert policy["audit_event"] == ("SELECT", "INSERT")
    assert policy["consent_record"] == ("SELECT", "INSERT")
    assert policy["suppression"] == ("SELECT", "INSERT", "UPDATE")
    assert policy["contacts_import_file"] == ("SELECT", "INSERT", "UPDATE")
    assert policy["contacts_segment"] == ("SELECT", "INSERT", "UPDATE", "DELETE")
    assert policy["django_session"] == ("SELECT", "INSERT", "UPDATE", "DELETE")
    assert policy["django_migrations"] == ("SELECT",)


def test_app_role_cannot_bypass_the_data_model(app_role):
    assert isinstance(as_role("SELECT count(*) FROM audit_event"), list)  # reading is fine
    for sql in ("UPDATE audit_event SET action = action",
                "DELETE FROM consent_record",
                "TRUNCATE audit_event",
                "TRUNCATE contacts_contact",
                "DELETE FROM suppression",
                "INSERT INTO django_migrations (app, name, applied) VALUES ('x', 'y', now())",
                "CREATE TABLE ep_test_x (i int)",
                "DROP TRIGGER audit_event_forbid_update ON audit_event"):  # fmt: skip
        result = as_role(sql)
        assert "permission denied" in result or "must be owner" in result, (sql, result)


def test_app_role_can_do_ordinary_work(app_role):
    assert as_role("DELETE FROM django_session WHERE false") == "ok"
    assert as_role("UPDATE contacts_contact SET first_name = first_name WHERE false") == "ok"
    # nextval on an integer primary key (email_templates uses serial ids).
    seq = as_role("SELECT pg_get_serial_sequence('email_templates_emailtemplate', 'id')")[0][0]
    assert isinstance(as_role("SELECT nextval(%s)", [seq]), list)


def test_problems_flags_the_owner_and_passes_the_app_role(app_role):
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(f"SET LOCAL ROLE {ROLE}")
        as_app = grants.problems()
        cur.execute("RESET ROLE")
    assert as_app == []
    assert grants.problems()  # the test connection is the superuser/owner


def test_deploy_check_warns_with_its_id():
    messages = grants.deploy_check()
    assert messages and all(m.id == "common.W010" for m in messages)


def test_command_prints_applies_and_refuses_the_owner(app_role):
    out = StringIO()
    call_command("db_grants", "--app-role", ROLE, stdout=out)
    assert 'GRANT SELECT, INSERT ON TABLE "audit_event" TO "ep_test_app_role";' in out.getvalue()
    assert "TRUNCATE" not in out.getvalue()
    with connection.cursor() as cur:
        cur.execute("SELECT current_user")
        me = cur.fetchone()[0]
    with pytest.raises(CommandError, match="different role"):
        call_command("db_grants", "--app-role", me)
    with pytest.raises(CommandError, match="does not exist"):
        call_command("db_grants", "--app-role", "no_such_role_ep")
    with pytest.raises(CommandError, match="privilege problem"):
        call_command("db_grants", "--check", stdout=StringIO())
