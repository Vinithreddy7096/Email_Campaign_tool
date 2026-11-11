"""Delivery pipeline (BUILD.md §10.7): expand → render → policy → SMTP, against a
scripted SMTP server so every reply path (250/4xx/5xx/lost reply) is exercised."""

import email
import smtplib
from datetime import timedelta
from email import policy as email_policy

import pytest
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from campaigns import services as campaigns
from common.models import Stream
from consent.models import ConsentRecord
from contacts.models import Contact, ContactList, ListMembership
from delivery.models import Message, PolicyDecision, RecipientJob, SendAttempt
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as templates
from suppression.models import Suppression
from workers.services import delivery
from workers.services.verp import parse_verp

pytestmark = [pytest.mark.integration, pytest.mark.django_db]
State = RecipientJob.State

HTML = ('<p>Hi {{ first_name }}</p><p>.dot line</p>'
        '<p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>')  # fmt: skip
TEXT = "Hi {{ first_name }}\n.starts with a dot\nUnsubscribe: {{ unsubscribe_url }}\n"


class FakeSMTP:
    """Scripted submission listener. Set FakeSMTP.script per test."""

    script: dict = {}
    sessions: list = []

    def __init__(self, host, port, timeout=None):
        if self.script.get("connect_error"):
            raise ConnectionRefusedError("refused")
        self.mail_from = self.rcpt_to = self.data = None
        FakeSMTP.sessions.append(self)

    def ehlo(self):
        return 250, b"hello"

    def mail(self, addr):
        self.mail_from = addr
        return self.script.get("mail", (250, b"2.1.0 Ok"))

    def rcpt(self, addr):
        self.rcpt_to = addr
        return self.script.get("rcpt", (250, b"2.1.5 Ok"))

    def docmd(self, cmd):
        assert cmd == "DATA"
        return 354, b"End data with <CR><LF>.<CR><LF>"

    def send(self, data):
        self.data = data

    def getreply(self):
        if self.script.get("lost_reply"):
            raise smtplib.SMTPServerDisconnected("connection lost")
        return self.script.get("reply", (250, b"2.0.0 Ok: queued as ABC123"))

    def quit(self):
        if self.script.get("quit_error"):
            raise smtplib.SMTPServerDisconnected("gone on QUIT")

    def close(self):
        pass

    def parsed(self):
        assert self.data.endswith(b"\r\n.\r\n")
        raw = self.data[: -len(b".\r\n")].replace(b"\r\n..", b"\r\n.")
        return email.message_from_bytes(raw, policy=email_policy.default)


@pytest.fixture(autouse=True)
def smtp(settings):
    settings.GLOBAL_SEND_ENABLED = True
    settings.MOCK_MODE = True
    settings.MESSAGE_ID_DOMAIN = "msgid.example.org"
    settings.BASE_URL = "https://mail.example.org"
    FakeSMTP.script = {}
    FakeSMTP.sessions = []
    return FakeSMTP


@pytest.fixture
def w(django_user_model):
    def person(email_addr, code):
        u = django_user_model.objects.create_user(email_addr, "pw", is_staff=True)
        UserRole.grant(u, code, by=None)
        return django_user_model.objects.get(pk=u.pk)

    admin = person("admin@example.com", Role.Code.SYSTEM_ADMIN)
    now = timezone.now()
    domain = SendingDomain.objects.create(
        domain="news.example.com", stream=Stream.MARKETING, return_path_domain="bounce.example.com",
        ip_pool=IpPool.objects.create(name="mkt", stream=Stream.MARKETING), status="ACTIVE",
        spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now,
    )  # fmt: skip
    sender = SenderIdentity.objects.create(domain=domain, local_part="news", display_name="News Desk",
                                           reply_to="help@example.com")  # fmt: skip
    lst = ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)
    members = []
    for i in range(3):
        c = Contact.objects.create(email=f"user{i}@example.net", first_name=f"P{i}")
        ListMembership.objects.create(contact=c, contact_list=lst)
        ConsentRecord.objects.create(contact=c, stream=Stream.MARKETING, action="GRANTED",
                                     method="DOUBLE_OPT_IN")  # fmt: skip
        members.append(c)
    _, version = templates.create_template(
        admin, name="Newsletter", category="marketing", subject="News for {{ first_name }}",
        html_body=HTML, text_body=TEXT, variables={"first_name": {"required": True}},
        sample_data={"first_name": "Ada"},
    )  # fmt: skip
    templates.submit(admin, version)
    version = templates.approve(admin, version)
    c = campaigns.create(admin, name="October", purpose=Stream.MARKETING, sender_identity=sender,
                         template_version=version, audience_list=lst)  # fmt: skip
    c = campaigns.approve(admin, campaigns.submit(admin, c))
    return {"admin": admin, "campaign": c, "members": members, "domain": domain, "version": version}


def schedule(w, at=None):
    return campaigns.schedule(w["admin"], w["campaign"], at or timezone.now()).campaign


def expand_and_render(w):
    schedule(w)
    assert delivery.dispatch_due_campaigns() == [w["campaign"].pk]
    ids = delivery.expand_campaign(w["campaign"].pk)
    for job_id in ids:
        delivery.render_job(job_id)
    return ids


def run(w, now=None):
    ids = expand_and_render(w)
    return [delivery.handoff_job(i, now=now, smtp_class=FakeSMTP) for i in ids]


def states(jobs):
    return sorted(RecipientJob.objects.get(pk=j.pk).state for j in jobs)


# ─── Happy path ──────────────────────────────────────────────────────────


def test_campaign_is_delivered_and_completes(w):
    jobs = run(w)
    assert states(jobs) == ["ACCEPTED"] * 3
    job = RecipientJob.objects.get(pk=jobs[0].pk)
    assert list(job.transitions.order_by("created_at").values_list("to_state", flat=True)) == [
        "ELIGIBLE", "QUEUED", "RENDERED", "SUBMITTED_TO_MTA", "ACCEPTED"]  # fmt: skip
    assert job.message.mta_queue_id == "ABC123" and job.send_attempts.get().outcome == "ACCEPTED"
    assert delivery.finalize_campaigns() == [w["campaign"].pk]
    w["campaign"].refresh_from_db()
    assert w["campaign"].status == "COMPLETED"
    assert {"campaign.dispatch", "campaign.complete"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )


def test_message_is_built_to_spec(w):
    run(w)
    session = next(s for s in FakeSMTP.sessions if s.rcpt_to == "user0@example.net")
    msg = session.parsed()
    assert msg["From"] == "News Desk <news@news.example.com>"
    assert msg["To"] == "user0@example.net" and msg["Reply-To"] == "help@example.com"
    assert msg["Subject"] == "News for P0" and msg["Date"]
    assert msg["Message-ID"].endswith("@msgid.example.org>")
    assert msg.get_content_type() == "multipart/alternative"
    text, html = msg.get_payload()
    assert (text.get_content_type(), html.get_content_type()) == ("text/plain", "text/html")
    assert text["Content-Transfer-Encoding"] == "quoted-printable"
    unsub = msg["List-Unsubscribe"].strip("<>")
    assert unsub.startswith("https://mail.example.org/u/")
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert unsub in text.get_content() and unsub in html.get_content()
    assert ".starts with a dot" in text.get_content()  # dot-stuffing round-trips
    job = RecipientJob.objects.get(recipient="user0@example.net")
    assert parse_verp(session.mail_from) == job.pk
    assert session.mail_from.endswith("@bounce.example.com")


def test_list_unsubscribe_is_literal_on_the_wire(w):
    """Regression: a long URL under the default 78-column policy was sent as
    =?utf-8?q?=3Chttps...?=, which providers do not decode. Check raw bytes."""
    run(w)
    raw = FakeSMTP.sessions[0].data
    lines = raw.split(b"\r\n")
    header = next(line for line in lines if line.startswith(b"List-Unsubscribe:"))
    assert header.startswith(b"List-Unsubscribe: <https://mail.example.org/u/")
    assert header.endswith(b">") and b"=?" not in header
    assert b"List-Unsubscribe-Post: List-Unsubscribe=One-Click" in lines


def test_message_id_falls_back_to_the_sending_domain(w, settings):
    settings.MESSAGE_ID_DOMAIN = ""
    run(w)
    assert FakeSMTP.sessions[0].parsed()["Message-ID"].endswith("@news.example.com>")


def test_expansion_and_handoff_are_idempotent(w):
    jobs = run(w)
    assert delivery.expand_campaign(w["campaign"].pk) == []
    assert RecipientJob.objects.count() == 3
    delivery.render_job(jobs[0].pk)
    delivery.handoff_job(jobs[0].pk, smtp_class=FakeSMTP)
    assert len(FakeSMTP.sessions) == 3 and Message.objects.count() == 3


def test_quit_failure_after_acceptance_is_still_accepted(w, smtp):
    smtp.script["quit_error"] = True
    assert states(run(w)) == ["ACCEPTED"] * 3


# ─── SMTP failures ───────────────────────────────────────────────────────


def test_4xx_defers_with_backoff_then_retries(w, smtp):
    now = timezone.now()
    smtp.script["reply"] = (451, b"4.3.0 try later")
    jobs = run(w, now=now)
    job = RecipientJob.objects.get(pk=jobs[0].pk)
    assert job.state == "DEFERRED" and job.next_attempt_at == now + timedelta(seconds=300)
    assert jobs[0].pk not in delivery.due_job_ids(now)
    assert jobs[0].pk in delivery.due_job_ids(now + timedelta(seconds=301))

    smtp.script.pop("reply")
    job = delivery.handoff_job(job.pk, now=now + timedelta(seconds=301), smtp_class=FakeSMTP)
    assert job.state == "ACCEPTED" and job.attempts == 2
    assert list(SendAttempt.objects.filter(job=job).order_by("number")
                .values_list("outcome", flat=True)) == ["DEFERRED", "ACCEPTED"]  # fmt: skip


def test_attempts_run_out(w, smtp, settings):
    settings.MAX_DELIVERY_ATTEMPTS = 2
    smtp.script["reply"] = (421, b"busy")
    now = timezone.now()
    job = run(w, now=now)[0]
    job = delivery.handoff_job(job.pk, now=now + timedelta(hours=1), smtp_class=FakeSMTP)
    assert job.state == "EXPIRED"


def test_5xx_on_recipient_bounces_without_suppressing(w, smtp):
    smtp.script["rcpt"] = (550, b"5.1.1 no such user")
    assert states(run(w)) == ["BOUNCED"] * 3
    assert not Suppression.objects.exists()  # the event pipeline decides (§10.8.2)


@pytest.mark.parametrize(
    "script", [{"mail": (553, b"sender not allowed")}, {"connect_error": True}]
)
def test_our_own_problems_defer_never_bounce(w, smtp, script):
    smtp.script.update(script)
    assert states(run(w)) == ["DEFERRED"] * 3


def test_lost_reply_after_data_is_held_for_review(w, smtp):
    smtp.script["lost_reply"] = True
    job = run(w)[0]
    job.refresh_from_db()
    assert job.state == "SUBMITTED_TO_MTA" and job.review_required
    assert job.send_attempts.get().outcome == "UNKNOWN"
    assert job.pk not in delivery.due_job_ids(timezone.now() + timedelta(days=1))

    smtp.script.pop("lost_reply")
    job = delivery.resolve_review(job.pk, "resend", actor=w["admin"], note="not in MTA log")
    assert job.state == "RENDERED" and not job.review_required
    job = delivery.handoff_job(job.pk, smtp_class=FakeSMTP)
    assert job.state == "ACCEPTED" and job.message.message_id.endswith(".2@msgid.example.org>")
    assert AuditEvent.objects.filter(action="delivery.review", actor=w["admin"]).exists()


@pytest.mark.parametrize(
    ("resolution", "state"), [("accepted", "ACCEPTED"), ("cancel", "CANCELED")]
)
def test_review_resolutions(w, smtp, resolution, state):
    smtp.script["lost_reply"] = True
    job = run(w)[0]
    assert delivery.resolve_review(job.pk, resolution, actor=w["admin"]).state == state


def test_worker_death_mid_submission_is_flagged(w):
    job_id = expand_and_render(w)[0]
    job = RecipientJob.objects.get(pk=job_id)
    with transaction.atomic():
        delivery._transition(job, State.SUBMITTED_TO_MTA, code="SUBMITTING")
        job.save()
    later = timezone.now() + timedelta(minutes=10)
    assert delivery.flag_stuck_submissions(later) == 1
    assert RecipientJob.objects.get(pk=job_id).review_required


# ─── Policy at handoff ───────────────────────────────────────────────────


def test_suppressed_after_approval_is_not_sent(w):
    Suppression.suppress("user1@example.net", Suppression.Reason.COMPLAINT)
    jobs = run(w)
    assert states(jobs) == ["ACCEPTED", "ACCEPTED", "SUPPRESSED"]
    assert "user1@example.net" not in {s.rcpt_to for s in FakeSMTP.sessions}


def test_hold_keeps_the_job_and_sends_nothing(w, settings):
    settings.GLOBAL_SEND_ENABLED = False
    now = timezone.now()
    jobs = run(w, now=now)
    assert states(jobs) == ["RENDERED"] * 3 and FakeSMTP.sessions == []
    job = RecipientJob.objects.get(pk=jobs[0].pk)
    assert job.next_attempt_at == now + timedelta(seconds=300)
    assert PolicyDecision.objects.filter(job_ref=str(job.pk), outcome="HOLD").exists()


def test_paused_campaign_holds_and_cancel_cancels(w):
    ids = expand_and_render(w)
    c = campaigns.pause(w["admin"], w["campaign"], "complaints")
    assert delivery.handoff_job(ids[0], smtp_class=FakeSMTP).state == "RENDERED"
    campaigns.cancel(w["admin"], c, "stop")
    delivery.finalize_campaigns()
    assert states(RecipientJob.objects.all()) == ["CANCELED"] * 3 and FakeSMTP.sessions == []


def test_erased_contact_gets_a_suppressed_job_and_campaign_completes(w):
    w["members"][2].erase()
    jobs = run(w)
    assert sorted(j.state for j in RecipientJob.objects.all()) == [
        "ACCEPTED",
        "ACCEPTED",
        "SUPPRESSED",
    ]
    erased = RecipientJob.objects.get(state="SUPPRESSED")
    assert erased.recipient == "" and erased.transitions.get().code == "CONTACT_ERASED"
    assert len(jobs) == 2 and delivery.finalize_campaigns() == [w["campaign"].pk]


def test_changed_contact_data_is_sent_fresh(w):
    ids = expand_and_render(w)
    job = RecipientJob.objects.get(pk=ids[0])
    old_hash = job.content_hash
    Contact.objects.filter(pk=job.contact_id).update(first_name="Renamed")
    job = delivery.handoff_job(job.pk, smtp_class=FakeSMTP)
    assert job.state == "ACCEPTED" and job.content_hash != old_hash
    assert job.message.content_hash == job.content_hash
    assert FakeSMTP.sessions[0].parsed()["Subject"] == "News for Renamed"


def test_too_old_jobs_expire(w, settings):
    ids = expand_and_render(w)
    later = timezone.now() + timedelta(hours=settings.MESSAGE_MAX_AGE_HOURS + 1)
    assert delivery.handoff_job(ids[0], now=later, smtp_class=FakeSMTP).state == "EXPIRED"


def test_metrics_report_job_states(client, w):
    from accounts.models import ApiToken

    run(w)
    token = ApiToken.issue(w["admin"], "t", scopes=["campaigns:read"])[1]
    body = client.get(reverse("api:campaign-metrics", args=[w["campaign"].pk]),
                      HTTP_AUTHORIZATION=f"Bearer {token}").json()  # fmt: skip
    assert body["jobs"] == {"ACCEPTED": 3}


# ─── Integrity and admin ─────────────────────────────────────────────────


def test_job_history_is_append_only(w):
    job = run(w)[0]
    for sql in ("UPDATE delivery_job_transition SET code = 'x' WHERE job_id = %s",
                "DELETE FROM delivery_send_attempt WHERE job_id = %s",
                "DELETE FROM delivery_message WHERE job_id = %s",
                "DELETE FROM delivery_recipient_job WHERE id = %s"):  # fmt: skip
        with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
            connection.cursor().execute(sql, [job.pk])


def test_invalid_transitions_are_refused(w):
    job = RecipientJob.objects.get(pk=run(w)[0].pk)
    with pytest.raises(delivery.InvalidJobTransition):
        delivery._transition(job, State.QUEUED, code="x")


def test_admin_review_actions(login_verified, w, smtp):
    smtp.script["lost_reply"] = True
    job = run(w)[0]
    client = login_verified(w["admin"])
    client.post(reverse("admin:delivery_recipientjob_changelist"),
                {"action": "mark_accepted", "_selected_action": [str(job.pk)]})  # fmt: skip
    assert RecipientJob.objects.get(pk=job.pk).state == "ACCEPTED"
    page = client.get(reverse("admin:delivery_recipientjob_change", args=[job.pk]))
    assert page.status_code == 200 and "REVIEW_ACCEPTED" in page.content.decode()
