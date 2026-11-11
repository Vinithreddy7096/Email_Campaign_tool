"""Event pipeline (BUILD.md §10.8): Postfix log, DSN and ARF emails, provider
webhooks → DeliveryEvent, with suppression and job effects."""

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from common.email import email_hash
from common.models import Stream
from config.celery import app as celery_app
from consent.evaluation import marketing_consent
from consent.models import ConsentRecord
from contacts.models import Contact
from delivery.models import Message, RecipientJob
from domains.models import SenderIdentity, SendingDomain
from email_templates import services as templates
from events.models import CampaignDeliveryStats, DeliveryEvent, EventReview, RawPayload
from suppression.models import Suppression
from workers.services import events
from workers.services.verp import verp_address

pytestmark = [pytest.mark.integration, pytest.mark.django_db]
QID = "4Xk9Pq1Zz7z"


@pytest.fixture
def admin(django_user_model):
    user = django_user_model.objects.create_user("ops@example.com", "pw", is_staff=True)
    UserRole.grant(user, Role.Code.SYSTEM_ADMIN, by=None)
    return django_user_model.objects.get(pk=user.pk)


@pytest.fixture
def job(admin):
    """One message the MTA has accepted, with Postfix queue id QID."""
    now = timezone.now()
    domain = SendingDomain.objects.create(domain="news.example.com", stream=Stream.MARKETING,
                                          return_path_domain="bounce.example.com", status="ACTIVE",
                                          spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now)  # fmt: skip
    sender = SenderIdentity.objects.create(domain=domain, local_part="news", display_name="News")
    _, v = templates.create_template(admin, name="T", category="marketing", subject="Hi",
                                     html_body='<a href="{{ unsubscribe_url }}">u</a>',
                                     text_body="{{ unsubscribe_url }}")  # fmt: skip
    templates.submit(admin, v)
    v = templates.approve(admin, v)
    contact = Contact.objects.create(email="dana@gmail.com")
    ConsentRecord.objects.create(
        contact=contact, stream=Stream.MARKETING, action="GRANTED", method="WEB_FORM"
    )
    j = RecipientJob.objects.create(
        idempotency_key="k1", purpose=Stream.MARKETING, contact=contact, recipient="dana@gmail.com",
        email_hash=contact.email_hash, sender_identity=sender, template_version=v,
        expected_checksum=v.checksum, state=RecipientJob.State.ACCEPTED,
    )  # fmt: skip
    Message.objects.create(job=j, message_id=f"<{j.pk.hex}.1@msgid.example.org>", mta_queue_id=QID,
                           envelope_from=verp_address(j.pk, "bounce.example.com"),
                           content_hash="x", size_bytes=1, accepted_at=now)  # fmt: skip
    return j


def postfix(status, dsn, resp, to="dana@gmail.com", qid=QID, ts="Oct 14 09:15:02"):
    return (
        f"{ts} mta1 postfix/smtp[4242]: {qid}: to=<{to}>, relay=gmail-smtp-in.l.google.com"
        f"[142.250.1.27]:25, delay=1.2, delays=0.1/0/0.4/0.7, dsn={dsn}, status={status} ({resp})"
    )


# ─── Postfix delivery log ────────────────────────────────────────────────


def test_delivered_line(job):
    counts = events.ingest_log_lines([postfix("sent", "2.0.0", "250 2.0.0 OK 1697 - gsmtp")])
    assert counts == {"events": 1, "duplicates": 0, "ignored": 0}
    e = DeliveryEvent.objects.get()
    assert (e.type, e.job, e.provider, e.smtp_code) == ("DELIVERED", job, "gmail", 250)
    assert e.email_hash == job.email_hash and "dana@gmail.com" not in e.diagnostic
    job.refresh_from_db()
    assert job.state == "ACCEPTED"


def test_ingest_is_idempotent(job):
    line = postfix("sent", "2.0.0", "250 OK")
    events.ingest_log_lines([line, line])
    assert events.ingest_log_lines([line]) == {"events": 0, "duplicates": 1, "ignored": 0}
    assert DeliveryEvent.objects.count() == 1


def test_deferral_after_acceptance_does_not_move_the_job(job):
    events.ingest_log_lines([postfix("deferred", "4.7.28", "host said: 421 4.7.28 rate limited")])
    job.refresh_from_db()
    assert job.state == "ACCEPTED"  # Postfix retries; the app must not resend
    assert DeliveryEvent.objects.get().type == "DEFERRED"


def test_hard_bounce_on_a_missing_mailbox_suppresses_everything(job):
    events.ingest_log_lines(
        [postfix("bounced", "5.1.1", "host said: 550 5.1.1 The email account does not exist")]
    )
    job.refresh_from_db()
    assert job.state == "BOUNCED"
    row = Suppression.objects.get()
    assert (row.scope, row.reason, row.email) == ("GLOBAL", "HARD_BOUNCE", None)
    assert Suppression.objects.blocking_for_send("dana@gmail.com", Stream.TRANSACTIONAL).exists()
    assert DeliveryEvent.objects.get().suppression == row


def test_policy_rejection_suppression_can_be_lifted(job, admin):
    """5.7.x is often about our reputation, not the recipient: suppress (Q-EVT-3),
    but on the stream, which an operator can lift — never the permanent GLOBAL."""
    events.ingest_log_lines([postfix("bounced", "5.7.1", "host said: 550 5.7.1 blocked by policy")])
    row = Suppression.objects.get()
    assert (row.scope, row.stream) == ("STREAM", "MARKETING")
    row.lift(by=admin, reason="reputation incident resolved")
    assert not Suppression.objects.blocking_for_send("dana@gmail.com", Stream.MARKETING).exists()


def test_expiry_is_a_soft_bounce_never_suppressed(job):
    events.ingest_log_lines([postfix("expired", "4.4.7", "delivery temporarily suspended")])
    job.refresh_from_db()
    assert job.state == "BOUNCED" and DeliveryEvent.objects.get().type == "SOFT_BOUNCE"
    assert not Suppression.objects.exists()  # Q-EVT-1


def test_reused_queue_id_for_another_recipient_is_not_linked(job):
    events.ingest_log_lines([postfix("sent", "2.0.0", "250 OK", to="someone@else.com")])
    assert DeliveryEvent.objects.get().job is None


def test_timestamps_and_noise():
    iso = postfix("sent", "2.0.0", "250 OK", qid="AAA1", ts="2026-10-14T09:15:02.123456+00:00")
    noise = "Oct 14 09:15:02 mta1 postfix/qmgr[1]: AAA1: removed"
    assert events.ingest_log_lines([iso, noise]) == {"events": 1, "duplicates": 0, "ignored": 1}
    assert DeliveryEvent.objects.get().occurred_at.isoformat().startswith("2026-10-14T09:15:02")


# ─── Bounce (DSN) and complaint (ARF) emails ─────────────────────────────


def dsn(
    to, recipient="dana@gmail.com", action="failed", status="5.1.1", mid="<dsn-1@mx.example.net>"
):
    return (
        f"""From: MAILER-DAEMON@mx.example.net
To: {to}
Subject: Undelivered Mail Returned to Sender
Message-ID: {mid}
Date: Tue, 14 Oct 2026 09:20:00 +0000
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain

Delivery to {recipient} failed.

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.net

Final-Recipient: rfc822; {recipient}
Action: {action}
Status: {status}
Diagnostic-Code: smtp; 550 {status} user unknown

--B--
"""
    ).encode()


def test_dsn_to_a_signed_verp_address_bounces_the_job(job):
    out = events.ingest_mail(dsn(verp_address(job.pk, "bounce.example.com")))
    assert [o.event.type for o in out] == ["HARD_BOUNCE"]
    job.refresh_from_db()
    assert job.state == "BOUNCED" and Suppression.objects.filter(scope="GLOBAL").exists()
    assert RawPayload.objects.get().processed_at is not None
    events.ingest_mail(dsn(verp_address(job.pk, "bounce.example.com")))  # re-delivered
    assert DeliveryEvent.objects.count() == 1


def test_forged_dsn_cannot_suppress_anyone(job):
    """Anyone can mail the bounce domain. Without our signed address: review only."""
    out = events.ingest_mail(dsn("b-" + "0" * 32 + "-" + "f" * 16 + "@bounce.example.com",
                                 recipient="victim@example.org"))  # fmt: skip
    assert out[0].event.type == "UNKNOWN"
    assert not Suppression.objects.exists()
    job.refresh_from_db()
    assert job.state == "ACCEPTED"


def test_dsn_delay_is_a_deferral(job):
    out = events.ingest_mail(dsn(verp_address(job.pk, "bounce.example.com"), action="delayed",
                                 status="4.2.2", mid="<dsn-2@mx>"))  # fmt: skip
    assert out[0].event.type == "DEFERRED"


def arf(original_headers, mid="<arf-1@fbl.example.com>"):
    return (
        f"""From: fbl@fbl.example.com
To: fbl@example.com
Subject: complaint
Message-ID: {mid}
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="B"

--B
Content-Type: text/plain

This is an abuse report.

--B
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: ExampleFBL/1.0
Version: 1

--B
Content-Type: text/rfc822-headers

{original_headers}

--B--
"""
    ).encode()


def test_complaint_with_our_return_path_suppresses_marketing(job):
    headers = f"Return-Path: <{verp_address(job.pk, 'bounce.example.com')}>\nTo: dana@gmail.com\nSubject: Hi"
    out = events.ingest_mail(arf(headers))
    assert out[0].event.type == "COMPLAINT" and out[0].event.source == "FBL"
    row = Suppression.objects.get()
    assert (row.scope, row.stream, row.reason) == ("STREAM", "MARKETING", "COMPLAINT")


def test_complaint_matched_by_our_message_id(job):
    mid = Message.objects.get(job=job).message_id
    out = events.ingest_mail(arf(f"Message-ID: {mid}\nTo: dana@gmail.com", mid="<arf-2@fbl>"))
    assert out[0].event.type == "COMPLAINT" and out[0].event.job == job


def test_unrecognised_mail_is_kept_for_review(job):
    out = events.ingest_mail(
        b"From: a@b.c\nTo: bounces@bounce.example.com\nSubject: out of office\n\nI'm away.\n"
    )
    assert out[0].event.type == "UNKNOWN" and RawPayload.objects.count() == 1
    assert not Suppression.objects.exists()


# ─── Provider webhook ────────────────────────────────────────────────────

SECRET = "test-webhook-secret"


@pytest.fixture
def eager(settings):
    settings.WEBHOOK_SIGNING_SECRET = SECRET
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


def signed(client, body, *, secret=SECRET, ts=None):
    raw = json.dumps(body).encode()
    ts = str(int(ts if ts is not None else time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    with transaction.atomic():  # run on_commit hooks inside the test transaction
        pass
    return client.post(reverse("api:events-provider"), raw, content_type="application/json",
                       HTTP_X_WEBHOOK_TIMESTAMP=ts, HTTP_X_WEBHOOK_SIGNATURE=sig)  # fmt: skip


def test_webhook_refuses_unsigned_stale_and_unconfigured(client, settings, job):
    settings.WEBHOOK_SIGNING_SECRET = ""
    assert signed(client, {"id": "e1"}).status_code == 503
    settings.WEBHOOK_SIGNING_SECRET = SECRET
    assert (
        client.post(
            reverse("api:events-provider"), "{}", content_type="application/json"
        ).status_code
        == 401
    )
    assert signed(client, {"id": "e1"}, secret="wrong").status_code == 401
    assert signed(client, {"id": "e1"}, ts=time.time() - 3600).status_code == 401
    assert not RawPayload.objects.exists()


def test_webhook_complaint_and_replay(client, eager, job, django_capture_on_commit_callbacks):
    mid = Message.objects.get(job=job).message_id
    body = {"id": "evt_123", "type": "complaint", "recipient": "dana@gmail.com", "message_id": mid}
    with django_capture_on_commit_callbacks(execute=True):
        resp = signed(client, body)
    assert resp.status_code == 202
    event = DeliveryEvent.objects.get()
    assert (event.type, event.source, event.job) == ("COMPLAINT", "WEBHOOK", job)
    assert Suppression.objects.get().reason == "COMPLAINT"
    with django_capture_on_commit_callbacks(execute=True):
        again = signed(client, body)
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    assert DeliveryEvent.objects.count() == 1


def test_webhook_unsubscribe_withdraws_consent(
    client, eager, job, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        signed(client, {"id": "evt_9", "type": "unsubscribe", "recipient": "Dana@Gmail.com"})
    assert marketing_consent(job.contact_id).code == "CONSENT_WITHDRAWN"
    assert Suppression.objects.get().reason == "UNSUBSCRIBE"


# ─── Review, telemetry, integrity ────────────────────────────────────────


def test_classifying_an_unknown_report(job, admin):
    headers = f"Return-Path: <{verp_address(job.pk, 'bounce.example.com')}>"
    unknown = events.ingest_mail(arf(headers, mid="<arf-3@fbl>").replace(b"Feedback-Type: abuse",
                                                                       b"Feedback-Type: other"))[0].event  # fmt: skip
    assert unknown.type == "UNKNOWN"
    events.resolve_unknown(
        unknown, EventReview.Resolution.HARD_BOUNCE, actor=admin, note="mailbox gone"
    )
    assert DeliveryEvent.objects.filter(source="OPERATOR", type="HARD_BOUNCE").exists()
    job.refresh_from_db()
    assert job.state == "BOUNCED" and Suppression.objects.exists()
    assert AuditEvent.objects.filter(action="event.review", actor=admin).exists()
    with pytest.raises(IntegrityError), transaction.atomic():  # reviewed once only
        events.resolve_unknown(unknown, EventReview.Resolution.IGNORE, actor=admin)


def test_unsubscribe_link_is_counted_once(client, job):
    from suppression import unsubscribe

    url = reverse("unsubscribe", args=[unsubscribe.make_token(job.contact_id, "")])
    client.post(url)
    client.post(url)
    assert DeliveryEvent.objects.filter(type="UNSUBSCRIBE", source="UNSUBSCRIBE_LINK").count() == 1


def test_campaign_stats_and_rate_alerts(job, admin, settings):
    from campaigns import services as campaigns
    from contacts.models import ContactList

    settings.COMPLAINT_RATE_ALERT_THRESHOLD = 0.01
    c = campaigns.create(admin, name="Oct", purpose="MARKETING", sender_identity=job.sender_identity,
                         template_version=job.template_version,
                         audience_list=ContactList.objects.create(name="L", stream="MARKETING"))  # fmt: skip
    RecipientJob.objects.filter(pk=job.pk).update(campaign=c)
    job.refresh_from_db()
    events.ingest_log_lines([postfix("sent", "2.0.0", "250 OK")])
    mid = Message.objects.get(job=job).message_id
    events.ingest_mail(arf(f"Message-ID: {mid}"))
    assert events.recompute_campaign_stats() == 1
    stats = CampaignDeliveryStats.objects.get(campaign=c)
    assert (stats.delivered, stats.complaints) == (1, 1)
    alerts = events.rate_alerts()
    assert alerts and alerts[0]["kind"] == "complaint"


def test_events_are_append_only(job):
    events.ingest_log_lines([postfix("sent", "2.0.0", "250 OK")])
    e = DeliveryEvent.objects.get()
    for sql in ("UPDATE events_delivery_event SET type = 'COMPLAINT' WHERE id = %s",
                "DELETE FROM events_delivery_event WHERE id = %s"):  # fmt: skip
        with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
            connection.cursor().execute(sql, [e.pk])


def test_ingest_commands(job, tmp_path, capsys, monkeypatch):
    import io
    import sys

    from django.core.management import call_command

    log = tmp_path / "mail.log"
    log.write_text(postfix("sent", "2.0.0", "250 OK") + "\nnot a delivery line\n", encoding="utf-8")
    call_command("ingest_postfix_log", str(log))
    assert "1 events recorded" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(dsn(verp_address(job.pk, "b.example.com"),
                                                                      mid="<dsn-cmd@mx>"))))  # fmt: skip
    call_command("ingest_mail")
    assert "HARD_BOUNCE recorded" in capsys.readouterr().out


def test_console_shows_and_classifies_reports(login_verified, job, admin):
    events.ingest_log_lines([postfix("bounced", "5.1.1", "550 5.1.1 no such user")])
    unknown = events.ingest_mail(b"From: a@b.c\nSubject: odd\n\nhm\n")[0].event
    client = login_verified(admin)
    page = client.get(reverse("console:events")).content.decode()
    assert "gmail" in page and "Unclassified reports · 1" in page
    today = client.get(reverse("console:today")).content.decode()
    assert "could not be classified" in today
    client.post(reverse("console:events"), {"event": unknown.pk, "classify": "IGNORE"})
    assert EventReview.objects.get(event=unknown).resolution == "IGNORE"


def test_hashes_never_addresses(job):
    events.ingest_log_lines(
        [postfix("bounced", "5.1.1", "550 5.1.1 <dana@gmail.com>: no such user")]
    )
    e = DeliveryEvent.objects.get()
    assert e.email_hash == email_hash("dana@gmail.com") and "dana@gmail.com" not in e.diagnostic
    assert e.occurred_at < timezone.now() + timedelta(days=1)
