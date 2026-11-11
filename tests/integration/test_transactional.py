"""Transactional API (BUILD.md §10.6.6): POST /api/v1/messages → one RecipientJob,
then the same delivery pipeline and policy as campaigns."""

import json

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken, Role, UserRole
from common.models import Stream
from delivery.models import RecipientJob, SendPause
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as templates
from suppression.models import Suppression
from workers.services import delivery

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def world(django_user_model):
    admin = django_user_model.objects.create_user("ops@example.com", "pw", is_staff=True)
    UserRole.grant(admin, Role.Code.SYSTEM_ADMIN, by=None)
    admin = django_user_model.objects.get(pk=admin.pk)
    now = timezone.now()
    txn = SendingDomain.objects.create(
        domain="notify.example.com", stream=Stream.TRANSACTIONAL, return_path_domain="b.example.com",
        ip_pool=IpPool.objects.create(name="txn", stream=Stream.TRANSACTIONAL), status="ACTIVE",
        spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now,
    )  # fmt: skip
    mkt = SendingDomain.objects.create(domain="news.example.com", stream=Stream.MARKETING,
                                       return_path_domain="b.example.com")  # fmt: skip
    SenderIdentity.objects.create(domain=txn, local_part="no-reply", display_name="Acme")
    SenderIdentity.objects.create(domain=mkt, local_part="news", display_name="News")
    for name, category, html, text in (
        ("Order receipt", "transactional", "<p>Order {{ order_id }}: {{ total }}</p>", "Order {{ order_id }}"),
        ("Newsletter", "marketing", '<a href="{{ unsubscribe_url }}">u</a>', "{{ unsubscribe_url }}"),
    ):  # fmt: skip
        variables = ({"order_id": {"required": True}, "total": {"required": True}}
                     if category == "transactional" else {})  # fmt: skip
        sample = {"order_id": "A-0", "total": "$1"} if category == "transactional" else {}
        _, v = templates.create_template(admin, name=name, category=category, subject=f"{name}",
                                         html_body=html, text_body=text, variables=variables,
                                         sample_data=sample)  # fmt: skip
        templates.submit(admin, v)
        templates.approve(admin, v)
    token = ApiToken.issue(admin, "storefront", scopes=["messages:send"])[1]
    return {"admin": admin, "auth": {"HTTP_AUTHORIZATION": f"Bearer {token}"}}


def post(client, world, **overrides):
    body = {"template": "Order receipt", "sender": "no-reply@notify.example.com",
            "to": "Dana@Example.org", "data": {"order_id": "A-1", "total": "$84.20"},
            "idempotency_key": "order-A-1"}  # fmt: skip
    body.update(overrides)
    auth = overrides.pop("auth", None) or world["auth"]
    return client.post(reverse("api:message-send"), json.dumps(body), content_type="application/json",
                       **auth)  # fmt: skip


def test_accepts_and_queues_one_job(client, world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        resp = post(client, world)
    assert resp.status_code == 202, resp.content
    job = RecipientJob.objects.get()
    assert (job.purpose, job.state, job.recipient) == (
        "TRANSACTIONAL",
        "QUEUED",
        "dana@example.org",
    )
    assert job.merge_data == {"order_id": "A-1", "total": "$84.20"}
    assert resp.json()["id"] == str(job.pk) and resp.json()["version"] == 1
    assert len(callbacks) == 1  # the render task is enqueued after commit


def test_same_key_same_body_is_the_same_message(client, world):
    first = post(client, world)
    again = post(client, world)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert RecipientJob.objects.count() == 1


def test_same_key_different_body_is_refused(client, world):
    post(client, world)
    resp = post(client, world, data={"order_id": "A-2", "total": "$1"})
    assert resp.status_code == 409 and resp.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert RecipientJob.objects.count() == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"template": "Nope"}, "TEMPLATE_UNKNOWN"),
        ({"template": "Newsletter"}, "TEMPLATE_NOT_TRANSACTIONAL"),
        ({"sender": "news@news.example.com"}, "SENDER_NOT_TRANSACTIONAL"),
        ({"sender": "who@notify.example.com"}, "SENDER_UNKNOWN"),
        ({"to": "not-an-address"}, "INVALID_RECIPIENT"),
        ({"data": {"order_id": "A-1"}}, "RENDER_FAILED"),
        ({"data": ["a"]}, "DATA_NOT_OBJECT"),
        ({"idempotency_key": ""}, "IDEMPOTENCY_KEY_REQUIRED"),
    ],
)
def test_rejections_name_the_problem(client, world, overrides, code):
    resp = post(client, world, **overrides)
    assert resp.status_code == 422 and resp.json()["code"] == code, resp.content
    assert not RecipientJob.objects.exists()


def test_token_and_scope_required(client, world):
    no_token = client.post(reverse("api:message-send"), "{}", content_type="application/json")
    assert no_token.status_code in (401, 403)
    other = ApiToken.issue(world["admin"], "ro", scopes=["campaigns:read"])[1]
    resp = post(client, world, auth={"HTTP_AUTHORIZATION": f"Bearer {other}"})
    assert resp.status_code == 403


def test_end_to_end_through_the_pipeline_and_status(client, world, settings):
    from tests.integration.test_delivery import FakeSMTP

    settings.GLOBAL_SEND_ENABLED = True
    FakeSMTP.script, FakeSMTP.sessions = {}, []
    job_id = post(client, world).json()["id"]
    delivery.render_job(job_id)
    job = delivery.handoff_job(job_id, smtp_class=FakeSMTP)
    assert job.state == "ACCEPTED"
    msg = FakeSMTP.sessions[0].parsed()
    assert msg["From"] == "Acme <no-reply@notify.example.com>" and "List-Unsubscribe" not in msg
    assert "Order A-1: $84.20" in msg.get_body(("html",)).get_content()
    status = client.get(reverse("api:message-detail", args=[job_id]), **world["auth"]).json()
    assert status["status"] == "ACCEPTED" and status["message_id"]
    assert [h["state"] for h in status["history"]][-1] == "ACCEPTED"


def test_suppressed_recipient_is_accepted_but_never_sent(client, world, settings):
    """Q-POL-2 default: every suppression applies to transactional mail."""
    settings.GLOBAL_SEND_ENABLED = True
    Suppression.suppress("dana@example.org", Suppression.Reason.UNSUBSCRIBE,
                         scope=Suppression.Scope.STREAM, stream=Stream.MARKETING)  # fmt: skip
    resp = post(client, world)
    assert resp.status_code == 202 and "suppressed" in resp.json()["warnings"][0]
    job_id = resp.json()["id"]
    delivery.render_job(job_id)
    assert delivery.handoff_job(job_id).state == "SUPPRESSED"


def test_pausing_the_api_client_holds_its_messages(client, world, settings):
    settings.GLOBAL_SEND_ENABLED = True
    job_id = post(client, world).json()["id"]
    job = RecipientJob.objects.get(pk=job_id)
    SendPause.pause("API_CLIENT", job.api_client_ref, reason="leaked token", by=world["admin"])
    delivery.render_job(job_id)
    assert delivery.handoff_job(job_id).state == "RENDERED"  # held, not sent


def test_status_is_private_to_the_sender(client, world, django_user_model):
    job_id = post(client, world).json()["id"]
    stranger = django_user_model.objects.create_user("x@example.com", "pw")
    token = ApiToken.issue(stranger, "x", scopes=["messages:read"])[1]
    resp = client.get(
        reverse("api:message-detail", args=[job_id]), HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    assert resp.status_code == 404


def test_rate_limit_when_configured(client, world, settings):
    settings.MESSAGES_API_RATE_PER_MINUTE = 2
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    from django.core.cache import cache

    cache.clear()
    codes = [post(client, world, idempotency_key=f"k{i}").status_code for i in range(3)]
    assert codes == [202, 202, 429]


def test_key_survives_token_rotation(client, world):
    first = post(client, world).json()["id"]
    rotated = ApiToken.issue(world["admin"], "storefront-2", scopes=["messages:send"])[1]
    again = post(client, world, auth={"HTTP_AUTHORIZATION": f"Bearer {rotated}"})
    assert again.status_code == 200 and again.json()["id"] == first


def test_status_says_why_a_message_is_waiting(client, world, settings):
    """A message held by a limit tells the caller why and when it is tried again."""
    from django.core.cache import cache

    from delivery.models import RateLimit
    from tests.integration.test_delivery import FakeSMTP

    settings.GLOBAL_SEND_ENABLED = True
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    FakeSMTP.script, FakeSMTP.sessions = {}, []
    RateLimit.add(by=world["admin"], scope="API_CLIENT", max_messages=1, per_seconds=3600)
    ids = [post(client, world, idempotency_key=f"k{i}").json()["id"] for i in range(2)]
    for job_id in ids:
        delivery.render_job(job_id)
        delivery.handoff_job(job_id, smtp_class=FakeSMTP)
    status = client.get(reverse("api:message-detail", args=[ids[1]]), **world["auth"]).json()
    assert status["status"] == "RENDERED"
    assert (
        status["held"]["reasons"][0]["code"] == "RATE_LIMITED" and status["held"]["next_attempt_at"]
    )
    done = client.get(reverse("api:message-detail", args=[ids[0]]), **world["auth"]).json()
    assert done["status"] == "ACCEPTED" and "held" not in done
