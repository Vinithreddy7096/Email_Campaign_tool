"""
Core data model (§6.2–§6.5, §6.10): the invariants that keep mail from going
where it must not. Each DB-level test bypasses the ORM on purpose — the
constraint has to hold even when application code is wrong.
"""

from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from audit.models import AuditEvent
from common.models import ImmutableRowError, Stream
from consent.models import ConsentRecord
from contacts.models import Contact, ContactImport, ContactList
from domains.models import IpPool, SenderIdentity, SendingDomain
from suppression.models import Suppression

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user("ops@example.com", "pw-not-used-here")


@pytest.fixture
def marketing_list():
    return ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)


def raw_sql(sql, params=()):
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(sql, params)


# ─── Domains ─────────────────────────────────────────────────────────────


def test_domain_is_normalized():
    d = SendingDomain.objects.create(
        domain="Mail.Example.COM", stream=Stream.MARKETING, return_path_domain="Bounce.Example.com"
    )
    assert d.domain == "mail.example.com"
    assert d.return_path_domain == "bounce.example.com"


def test_domain_cannot_go_active_without_dns():
    with pytest.raises(IntegrityError), transaction.atomic():
        SendingDomain.objects.create(
            domain="mail.example.com",
            stream=Stream.MARKETING,
            return_path_domain="bounce.example.com",
            status=SendingDomain.Status.ACTIVE,
        )


def test_domain_cannot_use_other_streams_pool():
    pool = IpPool.objects.create(name="txn", stream=Stream.TRANSACTIONAL)
    with pytest.raises(ValueError, match="must not share pools"):
        SendingDomain.objects.create(
            domain="news.example.com",
            stream=Stream.MARKETING,
            return_path_domain="bounce.example.com",
            ip_pool=pool,
        )


def test_sender_identity_from_is_always_on_its_domain():
    d = SendingDomain.objects.create(
        domain="mail.example.com", stream=Stream.MARKETING, return_path_domain="b.example.com"
    )
    s = SenderIdentity.objects.create(domain=d, local_part="News", display_name="News")
    assert s.from_address == "news@mail.example.com"
    with pytest.raises(ValueError):
        SenderIdentity.objects.create(domain=d, local_part="x@evil.com", display_name="x")


# ─── Contacts ────────────────────────────────────────────────────────────


def test_contact_email_is_unique_among_live_contacts():
    Contact.objects.create(email="bob@example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        Contact.objects.create(email="BOB@example.com")


def test_erasure_keeps_hash_and_frees_the_address():
    c = Contact.objects.create(email="bob@example.com", first_name="Bob", attributes={"x": 1})
    original_hash = c.email_hash
    c.erase()
    c.refresh_from_db()
    assert c.email is None and c.first_name == "" and c.attributes == {}
    assert c.email_hash == original_hash and c.deleted_at is not None
    # Re-subscribing later creates a fresh contact rather than reviving the erased one.
    Contact.objects.create(email="bob@example.com")


def test_contacts_are_never_hard_deleted():
    c = Contact.objects.create(email="bob@example.com")
    with pytest.raises(ImmutableRowError):
        c.delete()
    with pytest.raises(ImmutableRowError):
        Contact.objects.all().delete()


def test_import_requires_attestation(user, marketing_list):
    with pytest.raises(IntegrityError), transaction.atomic():
        ContactImport.objects.create(
            contact_list=marketing_list,
            file_object_key="imports/x.csv",
            original_filename="x.csv",
            consent_source="",
            attestation_text="",
            attested_by=user,
            attested_at=timezone.now(),
        )


# ─── Consent ─────────────────────────────────────────────────────────────


def _consent(contact, action, **kw):
    return ConsentRecord.objects.create(
        contact=contact,
        stream=Stream.MARKETING,
        action=action,
        method=ConsentRecord.Method.WEB_FORM,
        **kw,
    )


def test_latest_consent_record_wins():
    c = Contact.objects.create(email="bob@example.com")
    assert not ConsentRecord.objects.is_granted(c, Stream.MARKETING)
    _consent(c, ConsentRecord.Action.GRANTED, occurred_at=timezone.now() - timedelta(days=2))
    assert ConsentRecord.objects.is_granted(c, Stream.MARKETING)
    _consent(c, ConsentRecord.Action.WITHDRAWN)
    assert not ConsentRecord.objects.is_granted(c, Stream.MARKETING)


@pytest.mark.parametrize("order", ["grant_first", "withdraw_first"])
def test_a_withdrawal_wins_a_timestamp_tie(order):
    """Same occurred_at (coarse clock, batch import): fail closed, never send."""
    c = Contact.objects.create(email="bob@example.com")
    at = timezone.now()
    actions = [ConsentRecord.Action.GRANTED, ConsentRecord.Action.WITHDRAWN]
    for action in actions if order == "grant_first" else reversed(actions):
        _consent(c, action, occurred_at=at)
    assert not ConsentRecord.objects.is_granted(c, Stream.MARKETING)


def test_consent_max_age_applies_to_marketing(settings):
    settings.CONSENT_MAX_AGE_DAYS = 30
    c = Contact.objects.create(email="bob@example.com")
    _consent(c, ConsentRecord.Action.GRANTED, occurred_at=timezone.now() - timedelta(days=31))
    assert not ConsentRecord.objects.is_granted(c, Stream.MARKETING)


def test_consent_copies_email_hash_for_post_erasure_evidence():
    c = Contact.objects.create(email="bob@example.com")
    r = _consent(c, ConsentRecord.Action.GRANTED)
    assert r.email_hash == c.email_hash


def test_consent_is_append_only_in_orm_and_db():
    c = Contact.objects.create(email="bob@example.com")
    r = _consent(c, ConsentRecord.Action.GRANTED)
    r.action = ConsentRecord.Action.WITHDRAWN
    with pytest.raises(ImmutableRowError):
        r.save()
    with pytest.raises(ImmutableRowError):
        ConsentRecord.objects.filter(pk=r.pk).update(action="WITHDRAWN")
    with pytest.raises(DatabaseError, match="forbidden"):
        raw_sql("UPDATE consent_record SET action = 'WITHDRAWN' WHERE id = %s", [r.pk])
    with pytest.raises(DatabaseError, match="forbidden"):
        raw_sql("DELETE FROM consent_record WHERE id = %s", [r.pk])


# ─── Suppression ─────────────────────────────────────────────────────────


def test_global_suppression_blocks_every_stream():
    Suppression.suppress("Bob@Example.com", Suppression.Reason.COMPLAINT)
    assert Suppression.objects.blocks("bob@example.com", Stream.MARKETING)
    assert Suppression.objects.blocks("bob@example.com", Stream.TRANSACTIONAL)
    assert not Suppression.objects.blocks("alice@example.com", Stream.MARKETING)


def test_stream_and_list_scopes_are_narrow(marketing_list):
    Suppression.suppress(
        "bob@example.com",
        Suppression.Reason.UNSUBSCRIBE,
        scope=Suppression.Scope.STREAM,
        stream=Stream.MARKETING,
    )
    Suppression.suppress(
        "carol@example.com",
        Suppression.Reason.UNSUBSCRIBE,
        scope=Suppression.Scope.LIST,
        contact_list=marketing_list,
    )
    assert Suppression.objects.blocks("bob@example.com", Stream.MARKETING)
    assert not Suppression.objects.blocks("bob@example.com", Stream.TRANSACTIONAL)
    assert Suppression.objects.blocks("carol@example.com", Stream.MARKETING, marketing_list)
    assert not Suppression.objects.blocks("carol@example.com", Stream.MARKETING)


def test_suppress_is_idempotent():
    first, created = Suppression.suppress("bob@example.com", Suppression.Reason.HARD_BOUNCE)
    again, created_again = Suppression.suppress("BOB@example.com", Suppression.Reason.COMPLAINT)
    assert created and not created_again and first.pk == again.pk


def test_global_suppression_can_never_be_lifted(user):
    row, _ = Suppression.suppress("bob@example.com", Suppression.Reason.COMPLAINT)
    with pytest.raises(PermissionError):
        row.lift(by=user, reason="customer asked")
    # Even bypassing the ORM.
    with pytest.raises(IntegrityError):
        raw_sql(
            "UPDATE suppression SET lifted_at = now(), lifted_by_id = %s, "
            "lift_reason = 'x' WHERE id = %s",
            [user.pk, row.pk],
        )
    assert Suppression.objects.blocks("bob@example.com", Stream.TRANSACTIONAL)


def test_lifting_scoped_suppression_is_audited(user):
    row, _ = Suppression.suppress(
        "bob@example.com",
        Suppression.Reason.UNSUBSCRIBE,
        scope=Suppression.Scope.STREAM,
        stream=Stream.MARKETING,
    )
    row.lift(by=user, reason="re-subscribed via double opt-in")
    assert not Suppression.objects.blocks("bob@example.com", Stream.MARKETING)
    event = AuditEvent.objects.get(action="suppression.lift")
    assert event.actor == user and event.target_id == str(row.pk)


def test_suppression_rows_are_never_deleted():
    row, _ = Suppression.suppress("bob@example.com", Suppression.Reason.HARD_BOUNCE)
    with pytest.raises(ImmutableRowError):
        row.delete()
    with pytest.raises(DatabaseError, match="forbidden"):
        raw_sql("DELETE FROM suppression WHERE id = %s", [row.pk])


# ─── Audit ───────────────────────────────────────────────────────────────


def test_audit_log_is_insert_and_select_only():
    e = AuditEvent.record("test.event", note="hi")
    assert e.actor_label == "system"
    with pytest.raises(ImmutableRowError):
        AuditEvent.objects.all().delete()
    for sql in (
        "UPDATE audit_event SET action = 'tampered' WHERE id = %s",
        "DELETE FROM audit_event WHERE id = %s",
    ):
        with pytest.raises(DatabaseError, match="forbidden"):
            raw_sql(sql, [e.pk])
