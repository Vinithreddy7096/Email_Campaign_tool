"""Policy service (BUILD.md §10.9 checks 1–5): one sendable baseline, then each
rule broken on its own. Plus the fail-closed paths."""

from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from common.models import Stream
from consent.models import ConsentRecord
from contacts.models import Contact, ContactList
from delivery.models import PolicyDecision, SendPause
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as templates
from suppression.models import Suppression
from workers.services import policy
from workers.services.policy import SendRequest, decide

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

MARKETING_HTML = '<p>Hi {{ first_name }}</p><p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>'
MARKETING_TEXT = "Hi {{ first_name }}\nUnsubscribe: {{ unsubscribe_url }}\n"


@pytest.fixture(autouse=True)
def sending_on(settings):
    settings.GLOBAL_SEND_ENABLED = True
    settings.MOCK_MODE = True


@pytest.fixture
def admin_user(django_user_model):
    user = django_user_model.objects.create_user("admin@example.com", "pw", is_staff=True)
    UserRole.grant(user, Role.Code.SYSTEM_ADMIN, by=None)
    return django_user_model.objects.get(pk=user.pk)


def approved_version(author, *, name, category):
    html, text = (
        (MARKETING_HTML, MARKETING_TEXT) if category == "marketing" else ("<p>Hi</p>", "Hi")
    )
    _, version = templates.create_template(
        author, name=name, category=category, subject="Hello", html_body=html, text_body=text,
        variables={"first_name": {"required": False}}, sample_data={"first_name": "Ada"},
    )  # fmt: skip
    templates.submit(author, version)
    return templates.approve(author, version)


def make_domain(stream, name):
    now = timezone.now()
    pool = IpPool.objects.create(name=f"pool-{stream.lower()}", stream=stream)
    return SendingDomain.objects.create(
        domain=name, stream=stream, return_path_domain=f"bounce.{name}", ip_pool=pool,
        status=SendingDomain.Status.ACTIVE,
        spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now,
    )  # fmt: skip


@pytest.fixture
def world(admin_user):
    """A marketing send that passes every check."""
    domain = make_domain(Stream.MARKETING, "news.example.com")
    sender = SenderIdentity.objects.create(domain=domain, local_part="news", display_name="News")
    newsletter = ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)
    contact = Contact.objects.create(email="bob@example.com")
    ConsentRecord.objects.create(
        contact=contact, stream=Stream.MARKETING, action=ConsentRecord.Action.GRANTED,
        method=ConsentRecord.Method.DOUBLE_OPT_IN,
    )  # fmt: skip
    version = approved_version(admin_user, name="Newsletter", category="marketing")
    return {
        "admin": admin_user, "domain": domain, "sender": sender, "list": newsletter,
        "contact": contact, "version": version,
    }  # fmt: skip


def request(world, **overrides):
    fields = {
        "recipient": world["contact"].email,
        "purpose": Stream.MARKETING,
        "sender": world["sender"],
        "template_version": world["version"],
        "expected_checksum": world["version"].checksum,
        "from_snapshot": True,
        "contact": world["contact"],
        "contact_list": world["list"],
        "campaign_ref": "campaign-1",
        "job_ref": "job-1",
    }
    fields.update(overrides)
    return SendRequest(**fields)


def assert_outcome(decision, outcome, code=None):
    assert decision.outcome == outcome, decision.reasons
    if code is not None:
        assert code in decision.codes, decision.codes


# ─── Baseline and logging ────────────────────────────────────────────────


def test_baseline_sends_and_is_logged(world):
    decision = decide(request(world))
    assert decision.send and decision.reasons == ()
    row = PolicyDecision.objects.get(pk=decision.record_id)
    assert row.outcome == "SEND" and row.job_ref == "job-1" and row.reasons == []
    assert row.sending_domain == world["domain"] and row.template_version == world["version"]
    assert row.email_hash == world["contact"].email_hash


def test_every_failing_check_is_recorded_but_the_first_decides(world):
    """Kill switch (check 1) and suppression (check 2): HOLD now, both logged."""
    from django.conf import settings

    settings.GLOBAL_SEND_ENABLED = False
    Suppression.suppress("bob@example.com", Suppression.Reason.COMPLAINT)
    decision = decide(request(world))
    assert_outcome(decision, "HOLD")
    assert decision.codes == ["KILL_SWITCH", "SUPPRESSED"]
    assert PolicyDecision.objects.get(pk=decision.record_id).reasons[1]["code"] == "SUPPRESSED"


def test_decision_log_is_append_only_in_the_database(world):
    decision = decide(request(world))
    for sql in (
        "UPDATE delivery_policy_decision SET outcome = 'SEND' WHERE id = %s",
        "DELETE FROM delivery_policy_decision WHERE id = %s",
    ):
        with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
            connection.cursor().execute(sql, [decision.record_id])


# ─── Check 1: kill switch, pauses, routing ───────────────────────────────


def test_kill_switch(world, settings):
    settings.GLOBAL_SEND_ENABLED = False
    assert_outcome(decide(request(world)), "HOLD", "KILL_SWITCH")


@pytest.mark.parametrize(
    ("scope", "target"),
    [
        ("GLOBAL", lambda w: ""),
        ("STREAM", lambda w: Stream.MARKETING),
        ("DOMAIN", lambda w: str(w["domain"].pk)),
        ("IP_POOL", lambda w: str(w["domain"].ip_pool_id)),
        ("CAMPAIGN", lambda w: "campaign-1"),
    ],
)
def test_each_pause_scope_holds_and_resume_releases(world, scope, target):
    pause = SendPause.pause(scope, target(world), reason="incident 42", by=world["admin"])
    decision = decide(request(world))
    assert_outcome(decision, "HOLD", "PAUSED")
    assert "incident 42" in decision.reasons[0].detail
    pause.resume(by=world["admin"])
    assert decide(request(world)).send


def test_api_client_pause(world):
    SendPause.pause("API_CLIENT", "token-7", reason="leaked token", by=world["admin"])
    assert_outcome(decide(request(world, api_client_ref="token-7")), "HOLD", "PAUSED")
    assert decide(request(world, api_client_ref="token-8")).send


def test_pause_on_another_campaign_does_not_hold_this_one(world):
    SendPause.pause("CAMPAIGN", "campaign-2", reason="x", by=world["admin"])
    assert decide(request(world)).send


def test_pauses_are_audited_and_never_deleted(world):
    pause = SendPause.pause("GLOBAL", reason="complaint spike", by_label="auto:complaint-rate")
    pause.resume(by=world["admin"])
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"send.pause", "send.resume"} <= actions
    with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
        connection.cursor().execute("DELETE FROM delivery_send_pause WHERE id = %s", [pause.pk])


def test_non_production_without_mock_only_sends_to_allowlist(world, settings):
    settings.MOCK_MODE = False
    settings.ALLOWED_TEST_RECIPIENT_DOMAINS = ["example.test"]
    assert_outcome(decide(request(world)), "HOLD", "TEST_RECIPIENT_NOT_ALLOWED")
    Contact.objects.create(email="qa@example.test")
    allowed = decide(request(world, recipient="qa@example.test", contact=None,
                             purpose=Stream.MARKETING))  # fmt: skip
    assert "TEST_RECIPIENT_NOT_ALLOWED" not in allowed.codes


def test_no_ip_pool_holds_outside_mock_mode(world, settings):
    SendingDomain.objects.filter(pk=world["domain"].pk).update(ip_pool=None)
    assert decide(request(world)).send  # MOCK_MODE: routing ignored
    settings.MOCK_MODE = False
    settings.ALLOWED_TEST_RECIPIENT_DOMAINS = ["example.com"]
    assert_outcome(decide(request(world)), "HOLD", "NO_ROUTE")


def test_inactive_ip_pool_holds(world):
    IpPool.objects.filter(pk=world["domain"].ip_pool_id).update(is_active=False)
    assert_outcome(decide(request(world)), "HOLD", "IP_POOL_INACTIVE")


def test_marketing_mail_cannot_use_a_transactional_domain(world, admin_user):
    txn_domain = make_domain(Stream.TRANSACTIONAL, "notify.example.com")
    sender = SenderIdentity.objects.create(domain=txn_domain, local_part="n", display_name="N")
    assert_outcome(decide(request(world, sender=sender)), "HOLD", "STREAM_MISMATCH")


# ─── Check 2: suppression ────────────────────────────────────────────────


def test_global_address_suppression(world):
    Suppression.suppress("BOB@example.com", Suppression.Reason.HARD_BOUNCE)
    assert_outcome(decide(request(world)), "SUPPRESS", "SUPPRESSED")


def test_domain_suppression(world):
    Suppression.suppress_domain("Example.COM", Suppression.Reason.MANUAL)
    decision = decide(request(world))
    assert_outcome(decision, "SUPPRESS", "SUPPRESSED")
    assert "@example.com" in decision.reasons[0].detail


def test_other_list_suppression_does_not_block_marketing_to_this_list(world):
    other = ContactList.objects.create(name="Other", stream=Stream.MARKETING)
    Suppression.suppress("bob@example.com", Suppression.Reason.UNSUBSCRIBE,
                         scope=Suppression.Scope.LIST, contact_list=other)  # fmt: skip
    assert decide(request(world)).send


def test_transactional_honours_every_suppression(world, admin_user):
    """Q-POL-2 default: all suppressions apply to transactional mail too."""
    txn_domain = make_domain(Stream.TRANSACTIONAL, "notify.example.com")
    sender = SenderIdentity.objects.create(domain=txn_domain, local_part="n", display_name="N")
    version = approved_version(admin_user, name="Receipt", category="transactional")
    txn = request(world, purpose=Stream.TRANSACTIONAL, sender=sender, template_version=version,
                  expected_checksum=version.checksum, from_snapshot=False, contact_list=None)  # fmt: skip
    assert decide(txn).send
    Suppression.suppress("bob@example.com", Suppression.Reason.UNSUBSCRIBE,
                         scope=Suppression.Scope.STREAM, stream=Stream.MARKETING)  # fmt: skip
    assert_outcome(decide(txn), "SUPPRESS", "SUPPRESSED")


def test_lifted_suppression_no_longer_blocks(world):
    row, _ = Suppression.suppress("bob@example.com", Suppression.Reason.UNSUBSCRIBE,
                                  scope=Suppression.Scope.STREAM, stream=Stream.MARKETING)  # fmt: skip
    assert not decide(request(world)).send
    row.lift(by=world["admin"], reason="re-subscribed")
    assert decide(request(world)).send


def test_invalid_address_and_erased_contact(world):
    assert_outcome(
        decide(request(world, recipient="not-an-address")), "SUPPRESS", "INVALID_ADDRESS"
    )
    assert_outcome(decide(request(world, recipient=None)), "SUPPRESS", "INVALID_ADDRESS")
    address = world["contact"].email
    world["contact"].erase()  # a job queued before erasure still holds the address
    assert_outcome(decide(request(world, recipient=address)), "SUPPRESS", "CONTACT_ERASED")


# ─── Check 3: consent ────────────────────────────────────────────────────


def test_marketing_without_consent_is_suppressed(world):
    stranger = Contact.objects.create(email="alice@example.com")
    decision = decide(request(world, recipient="alice@example.com", contact=stranger))
    assert_outcome(decision, "SUPPRESS", "NO_CONSENT")
    no_contact = decide(request(world, recipient="alice@example.com", contact=None))
    assert_outcome(no_contact, "SUPPRESS", "NO_CONSENT")


def test_list_scoped_consent_is_enough(world):
    member = Contact.objects.create(email="carol@example.com")
    ConsentRecord.objects.create(contact=member, stream=Stream.MARKETING,
                                 contact_list=world["list"], action="GRANTED", method="WEB_FORM")  # fmt: skip
    assert decide(request(world, recipient=member.email, contact=member)).send


def test_any_current_withdrawal_wins(world):
    """List grant + stream-wide withdrawal → no marketing at all (fail closed)."""
    ConsentRecord.objects.create(contact=world["contact"], stream=Stream.MARKETING,
                                 contact_list=world["list"], action="GRANTED", method="WEB_FORM")  # fmt: skip
    ConsentRecord.objects.create(contact=world["contact"], stream=Stream.MARKETING,
                                 action="WITHDRAWN", method="UNSUBSCRIBE_LINK")  # fmt: skip
    assert_outcome(decide(request(world)), "SUPPRESS", "CONSENT_WITHDRAWN")


def test_expired_consent(world, settings):
    settings.CONSENT_MAX_AGE_DAYS = 30
    old = Contact.objects.create(email="dave@example.com")
    ConsentRecord.objects.create(contact=old, stream=Stream.MARKETING, action="GRANTED",
                                 method="WEB_FORM", occurred_at=timezone.now() - timedelta(days=31))  # fmt: skip
    assert_outcome(
        decide(request(world, recipient=old.email, contact=old)), "SUPPRESS", "CONSENT_EXPIRED"
    )


# ─── Check 4: domain and sender ──────────────────────────────────────────


@pytest.mark.parametrize("status", ["PAUSED", "VERIFIED", "PENDING_VERIFICATION", "RETIRED"])
def test_domain_must_be_active(world, status):
    SendingDomain.objects.filter(pk=world["domain"].pk).update(status=status)
    assert_outcome(decide(request(world)), "HOLD", "DOMAIN_NOT_ACTIVE")


def test_stale_objects_cannot_send(world):
    """The caller's in-memory domain says ACTIVE; the database says PAUSED."""
    SendingDomain.objects.filter(pk=world["domain"].pk).update(status="PAUSED")
    assert world["sender"].domain.status == "ACTIVE"
    assert_outcome(decide(request(world)), "HOLD", "DOMAIN_NOT_ACTIVE")


def test_inactive_sender(world):
    SenderIdentity.objects.filter(pk=world["sender"].pk).update(is_active=False)
    assert_outcome(decide(request(world)), "HOLD", "SENDER_INACTIVE")


# ─── Check 5: template governance ────────────────────────────────────────


def test_retired_version_finishes_a_campaign_but_starts_nothing_new(world):
    templates.retire(world["admin"], world["version"])
    assert decide(request(world, from_snapshot=True)).send
    assert_outcome(decide(request(world, from_snapshot=False)), "HOLD", "TEMPLATE_NOT_APPROVED")


def test_draft_version_never_sends(world):
    draft = templates.new_version_from(world["admin"], world["version"])
    decision = decide(request(world, template_version=draft, expected_checksum=draft.checksum))
    assert_outcome(decision, "HOLD", "TEMPLATE_NOT_APPROVED")


def test_checksum_must_match_snapshot(world):
    decision = decide(request(world, expected_checksum="0" * 64))
    assert_outcome(decision, "HOLD", "TEMPLATE_CHECKSUM_MISMATCH")


def test_content_edited_behind_the_models_back(world):
    with connection.cursor() as cur:
        cur.execute("UPDATE email_templates_templateversion SET subject = 'Win $$$' WHERE id = %s",
                    [world["version"].pk])  # fmt: skip
    assert_outcome(decide(request(world)), "HOLD", "TEMPLATE_TAMPERED")


def test_transactional_template_cannot_go_out_as_marketing(world, admin_user):
    receipt = approved_version(admin_user, name="Receipt", category="transactional")
    decision = decide(request(world, template_version=receipt, expected_checksum=receipt.checksum))
    assert_outcome(decision, "HOLD", "TEMPLATE_PURPOSE_MISMATCH")


# ─── Fail closed ─────────────────────────────────────────────────────────


def test_a_crashing_check_holds(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(Suppression.objects, "blocking_for_send", boom)
    decision = decide(request(world))
    assert_outcome(decision, "HOLD", "POLICY_ERROR")
    assert decision.reasons[0].check == 2


def test_unloadable_request_holds(world):
    world["sender"].pk = world["sender"].pk.__class__(int=0)  # no such row
    assert_outcome(decide(request(world)), "HOLD", "POLICY_ERROR")


def test_send_that_cannot_be_logged_becomes_hold(world, monkeypatch):
    def fail(**kwargs):
        raise DatabaseError("disk full")

    monkeypatch.setattr(PolicyDecision.objects, "create", fail)
    decision = decide(request(world))
    assert_outcome(decision, "HOLD", "POLICY_LOG_FAILED")
    assert decision.record_id is None


def test_suppress_that_cannot_be_logged_stays_suppress(world, monkeypatch):
    Suppression.suppress("bob@example.com", Suppression.Reason.COMPLAINT)
    monkeypatch.setattr(PolicyDecision.objects, "create",
                        lambda **kw: (_ for _ in ()).throw(DatabaseError("x")))  # fmt: skip
    assert_outcome(decide(request(world)), "SUPPRESS", "SUPPRESSED")


def test_checks_run_in_spec_order():
    assert [n for n, _ in policy.CHECKS] == [1, 2, 3, 4, 5]


# ─── Admin ───────────────────────────────────────────────────────────────


def test_admin_pause_and_suppression_form(login_verified, world):
    from django.urls import reverse

    client = login_verified(world["admin"])
    client.post(reverse("admin:delivery_sendpause_add"),
                {"scope": "GLOBAL", "target": "", "reason": "maintenance"})  # fmt: skip
    pause = SendPause.objects.active().get()
    assert pause.created_by == world["admin"]
    assert_outcome(decide(request(world)), "HOLD", "PAUSED")

    resp = client.post(reverse("admin:suppression_suppression_add"),
                       {"email": "x@example.com", "domain": "example.com", "scope": "GLOBAL",
                        "reason": "MANUAL"})  # fmt: skip
    assert resp.status_code == 200 and "not both" in resp.content.decode()
    assert not Suppression.objects.exists()

    for name in ("delivery_sendpause", "delivery_policydecision"):
        assert client.get(reverse(f"admin:{name}_changelist")).status_code == 200
