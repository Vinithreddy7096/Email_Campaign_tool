"""MTA-facing endpoints (BUILD.md §10.8.1, §10.10): signed bounce and log intake,
the map export the Ansible roles render from, /metrics and the probe paths."""

import hashlib
import hmac
import json
import time
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from common.models import Stream
from domains.models import SendingDomain
from events.models import DeliveryEvent, RawPayload

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

SECRET = "m" * 40
DSN = (b"From: MAILER-DAEMON@remote.example.org\r\nTo: b-x@bounce.example.com\r\n"
       b"Subject: Undelivered\r\nMessage-ID: <dsn-1@remote.example.org>\r\n\r\nbounced\r\n")  # fmt: skip
LINE = ("2026-10-15T12:00:00+00:00 mta1 postfix-marketing/smtp[42]: ABC123: to=<dana@example.org>, "
        "relay=mx.example.org[192.0.2.1]:25, delay=1, delays=0/0/0/1, dsn=2.0.0, status=sent (250 Ok)")  # fmt: skip


@pytest.fixture(autouse=True)
def secrets(settings):
    settings.MTA_INGEST_SECRET = SECRET
    settings.WEBHOOK_SIGNING_SECRET = "w" * 40
    settings.METRICS_TOKEN = "t" * 40


def signed(body, secret=SECRET, stamp=None):
    stamp = stamp or str(int(time.time()))
    sig = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"HTTP_X_WEBHOOK_TIMESTAMP": stamp, "HTTP_X_WEBHOOK_SIGNATURE": sig}


def post_mail(client, body=DSN, **headers):
    return client.post(reverse("api:events-mail"), body, content_type="message/rfc822",
                       **(headers or signed(body)))  # fmt: skip


def post_lines(client, lines, **headers):
    body = json.dumps({"lines": lines}).encode()
    return client.post(reverse("api:events-postfix-log"), body, content_type="application/json",
                       **(headers or signed(body)))  # fmt: skip


def test_a_signed_bounce_is_stored_once(client):
    assert post_mail(client).status_code == 200
    assert post_mail(client).status_code == 200  # Postfix retried: nothing new
    assert RawPayload.objects.count() == 1


def test_the_provider_webhook_secret_does_not_open_mta_intake(client):
    assert post_mail(client, **signed(DSN, secret="w" * 40)).status_code == 401
    assert not RawPayload.objects.exists()


def test_a_bad_signature_is_refused(client):
    headers = {"HTTP_X_WEBHOOK_TIMESTAMP": str(int(time.time())), "HTTP_X_WEBHOOK_SIGNATURE": "00"}
    assert post_mail(client, **headers).status_code == 401


def test_a_stale_signature_is_refused(client):
    assert post_mail(client, **signed(DSN, stamp=str(int(time.time()) - 3600))).status_code == 401


def test_intake_is_off_without_a_secret(client, settings):
    settings.MTA_INGEST_SECRET = ""
    assert post_mail(client, **signed(DSN, secret="")).status_code == 503


def test_log_lines_become_events(client):
    resp = post_lines(client, [LINE, "connect from unknown[192.0.2.9]"])
    assert resp.status_code == 200 and resp.json()["events"] == 1 and resp.json()["ignored"] == 1
    assert DeliveryEvent.objects.get().type == "DELIVERED"
    assert post_lines(client, [LINE]).json()["duplicates"] == 1


def test_log_intake_limits(client):
    assert post_lines(client, ["x"] * 1001).status_code == 413
    body = b'{"lines": "not a list"}'
    resp = client.post(
        reverse("api:events-postfix-log"), body, content_type="application/json", **signed(body)
    )
    assert resp.status_code == 400


# ─── export_mta_maps ─────────────────────────────────────────────────────


def export():
    out = StringIO()
    call_command("export_mta_maps", stdout=out)
    return json.loads(out.getvalue())


def test_export_lists_signing_routes_and_bounce_domains(settings):
    settings.DEFAULT_RETURN_PATH_DOMAIN = "bounce.example.com"
    SendingDomain.objects.create(domain="news.example.com", stream=Stream.MARKETING,
                                 return_path_domain="bounce.news.example.com", dkim_selector="s1")  # fmt: skip
    SendingDomain.objects.create(domain="notify.example.com", stream=Stream.TRANSACTIONAL,
                                 return_path_domain="bounce.notify.example.com", dkim_selector="s1")  # fmt: skip
    SendingDomain.objects.create(domain="old.example.com", stream=Stream.MARKETING,
                                 return_path_domain="bounce.old.example.com", status="RETIRED")  # fmt: skip
    maps = export()
    assert maps["signing"] == [{"domain": "news.example.com", "selector": "s1"},
                               {"domain": "notify.example.com", "selector": "s1"}]  # fmt: skip
    assert maps["sender_transport"] == [
        {"return_path_domain": "bounce.news.example.com", "stream": "marketing"},
        {"return_path_domain": "bounce.notify.example.com", "stream": "transactional"},
    ]
    assert maps["bounce_domains"] == ["bounce.example.com", "bounce.news.example.com",
                                      "bounce.notify.example.com"]  # fmt: skip


def test_export_refuses_a_return_path_shared_across_streams():
    SendingDomain.objects.create(domain="a.example.com", stream=Stream.MARKETING,
                                 return_path_domain="bounce.example.com")  # fmt: skip
    SendingDomain.objects.create(domain="b.example.com", stream=Stream.TRANSACTIONAL,
                                 return_path_domain="bounce.example.com")  # fmt: skip
    with pytest.raises(CommandError, match="shared"):
        export()


# ─── /metrics and probes ─────────────────────────────────────────────────


def test_metrics_need_the_token(client):
    assert client.get("/metrics").status_code == 404
    assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer wrong").status_code == 404
    resp = client.get("/metrics", HTTP_AUTHORIZATION=f"Bearer {'t' * 40}")
    assert resp.status_code == 200
    text = resp.content.decode()
    for name in ('mailer_jobs{state="QUEUED"}', "mailer_global_send_enabled", "mailer_handoff_lag_seconds",
                 "mailer_rate_limits_configured", "mailer_task_last_run_timestamp_seconds"):  # fmt: skip
        assert name in text, name


def test_metrics_are_off_without_a_token(client, settings):
    settings.METRICS_TOKEN = ""
    assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer ").status_code == 404


def test_probes_answer_by_ip_but_nothing_else_does(client):
    """Load balancers and Prometheus use the container IP as Host."""
    token = f"Bearer {'t' * 40}"
    assert client.get("/health/live", HTTP_HOST="10.9.9.9").status_code == 200
    assert client.get("/metrics", HTTP_HOST="10.9.9.9", HTTP_AUTHORIZATION=token).status_code == 200
    assert client.get("/login/", HTTP_HOST="10.9.9.9").status_code == 400
