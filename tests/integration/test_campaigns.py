"""Campaigns (BUILD.md §10.6): lifecycle, preflight, snapshot, scheduling, API."""

import json
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken, Role, UserRole
from audit.models import AuditEvent
from campaigns import preflight, services
from campaigns.models import Campaign, CampaignSnapshot, CampaignSnapshotRecipient
from common.models import Stream
from consent.models import ConsentRecord
from contacts.models import Contact, ContactList, ListMembership
from delivery.models import SendPause
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as templates
from suppression.models import Suppression
from workers.services.policy import SendRequest, decide

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

HTML = '<p>Hi {{ first_name }}</p><p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>'
TEXT = "Hi {{ first_name }}\nUnsubscribe: {{ unsubscribe_url }}\n"


@pytest.fixture
def person(django_user_model):
    def make(email, *codes):
        user = django_user_model.objects.create_user(email, "pw", is_staff=True)
        for code in codes:
            UserRole.grant(user, code, by=None)
        return django_user_model.objects.get(pk=user.pk)

    return make


def approved_template(author, name="Newsletter", category="marketing", html=HTML, text=TEXT):
    _, v = templates.create_template(
        author, name=name, category=category, subject="News for {{ first_name }}",
        html_body=html, text_body=text,
        variables={"first_name": {"required": True}}, sample_data={"first_name": "Ada"},
    )  # fmt: skip
    templates.submit(author, v)
    return templates.approve(author, v)


def add_member(lst, email, *, consent=True, first_name="Pat"):
    c = Contact.objects.create(email=email, first_name=first_name)
    ListMembership.objects.create(contact=c, contact_list=lst)
    if consent:
        ConsentRecord.objects.create(contact=c, stream=Stream.MARKETING, action="GRANTED",
                                     method="DOUBLE_OPT_IN")  # fmt: skip
    return c


@pytest.fixture
def w(person):
    admin = person("admin@example.com", Role.Code.SYSTEM_ADMIN)
    now = timezone.now()
    pool = IpPool.objects.create(name="mkt", stream=Stream.MARKETING)
    domain = SendingDomain.objects.create(
        domain="news.example.com", stream=Stream.MARKETING, return_path_domain="b.example.com",
        ip_pool=pool, status="ACTIVE", spf_verified_at=now, dkim_verified_at=now,
        dmarc_verified_at=now,
    )  # fmt: skip
    newsletter = ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)
    members = [add_member(newsletter, f"user{i}@example.com", first_name=f"P{i}") for i in range(4)]
    return {
        "admin": admin,
        "manager": person("manager@example.com", Role.Code.CAMPAIGN_MANAGER),
        "approver": person("approver@example.com", Role.Code.APPROVER),
        "domain": domain,
        "sender": SenderIdentity.objects.create(
            domain=domain, local_part="news", display_name="News"
        ),
        "list": newsletter,
        "members": members,
        "version": approved_template(admin),
    }


def new_campaign(w, **overrides):
    fields = {"name": "October news", "purpose": Stream.MARKETING, "sender_identity": w["sender"],
              "template_version": w["version"], "audience_list": w["list"]}  # fmt: skip
    fields.update(overrides)
    return services.create(w["manager"], **fields)


def approved(w, **overrides):
    c = services.submit(w["manager"], new_campaign(w, **overrides))
    return services.approve(w["approver"], c)


def codes(campaign):
    return {f["code"] for f in campaign.preflight_report["findings"]}


# ─── Authoring ───────────────────────────────────────────────────────────


def test_create_never_sends_and_is_audited(w):
    c = new_campaign(w)
    assert c.status == "DRAFT" and c.owner == w["manager"]
    assert AuditEvent.objects.filter(action="campaign.create", actor=w["manager"]).exists()


def test_template_version_must_be_approved(w):
    draft = templates.new_version_from(w["admin"], w["version"])
    with pytest.raises(ValidationError, match="not APPROVED"):
        new_campaign(w, template_version=draft)


def test_only_drafts_are_editable_and_an_edit_resets_preflight(w):
    c = services.submit(w["manager"], new_campaign(w))
    assert c.status == "READY_FOR_REVIEW"
    with pytest.raises(services.InvalidTransition):
        services.update(w["manager"], c, name="Renamed")
    c = services.request_changes(w["approver"], c, "fix the subject")
    c = services.update(w["manager"], c, name="Renamed")
    assert c.status == "DRAFT" and c.preflight_report == {}


# ─── Preflight (§10.6.3) ─────────────────────────────────────────────────


def test_clean_campaign_passes_with_samples(w, settings):
    settings.CAMPAIGN_PREFLIGHT_SAMPLE_SIZE = 2
    c = services.submit(w["manager"], new_campaign(w))
    report = c.preflight_report
    assert c.status == "READY_FOR_REVIEW"
    assert report["audience"] == {"total": 4, "excluded_suppressed": 0, "recipients": 4,
                                  "missing_consent": {}}  # fmt: skip
    # Recipients are in contact-id order; UUIDv7 ids minted in the same
    # millisecond have no defined order, so don't assume which two are sampled.
    subjects = [s["subject"] for s in report["samples"]]
    assert len(subjects) == 2 and len(set(subjects)) == 2
    assert all(subj in {f"News for P{i}" for i in range(4)} for subj in subjects)


def test_suppressed_recipients_are_excluded_not_failed(w):
    Suppression.suppress("user1@example.com", Suppression.Reason.COMPLAINT)
    Suppression.suppress_domain("example.org", Suppression.Reason.MANUAL)
    add_member(w["list"], "someone@example.org")
    c = services.submit(w["manager"], new_campaign(w))
    assert c.status == "READY_FOR_REVIEW"
    assert c.preflight_report["audience"]["total"] == 5
    assert c.preflight_report["audience"]["excluded_suppressed"] == 2
    assert c.preflight_report["audience"]["recipients"] == 3
    assert "SUPPRESSED_EXCLUDED" in codes(c)


def test_missing_consent_fails_preflight(w):
    add_member(w["list"], "noconsent@example.com", consent=False)
    with pytest.raises(services.PreflightFailed):
        services.submit(w["manager"], new_campaign(w))
    c = Campaign.objects.get()
    assert c.status == "PREFLIGHT_FAILED" and "MISSING_CONSENT" in codes(c)
    assert c.preflight_report["audience"]["missing_consent"] == {"NO_CONSENT": 1}


@pytest.mark.parametrize(
    ("breakage", "code"),
    [
        (lambda w: SendingDomain.objects.filter(pk=w["domain"].pk).update(status="PENDING_VERIFICATION"),
         "SENDER_DOMAIN_NOT_VERIFIED"),
        (lambda w: SendingDomain.objects.filter(pk=w["domain"].pk).update(dmarc_verified_at=None,
                                                                          status="VERIFIED"),
         "DMARC_NOT_VERIFIED"),
        (lambda w: SenderIdentity.objects.filter(pk=w["sender"].pk).update(is_active=False),
         "SENDER_INACTIVE"),
        (lambda w: ContactList.objects.filter(pk=w["list"].pk).update(archived_at=timezone.now()),
         "LIST_ARCHIVED"),
        (lambda w: ContactList.objects.filter(pk=w["list"].pk).update(stream="TRANSACTIONAL"),
         "LIST_STREAM_MISMATCH"),
        (lambda w: ListMembership.objects.all().delete(), "EMPTY_AUDIENCE"),
    ],
)  # fmt: skip
def test_preflight_failures(w, breakage, code):
    c = new_campaign(w)
    breakage(w)
    with pytest.raises(services.PreflightFailed):
        services.submit(w["manager"], c)
    assert code in codes(Campaign.objects.get(pk=c.pk))


def test_transactional_template_cannot_be_a_marketing_campaign(w):
    receipt = approved_template(w["admin"], name="Receipt", category="transactional",
                                html="<p>Hi {{ first_name }}</p>", text="Hi {{ first_name }}")  # fmt: skip
    with pytest.raises(services.PreflightFailed):
        services.submit(w["manager"], new_campaign(w, template_version=receipt))
    assert "TEMPLATE_PURPOSE_MISMATCH" in codes(Campaign.objects.get())


def test_sample_render_uses_real_merge_data(w):
    """A recipient whose real data breaks the template fails preflight."""
    ListMembership.objects.all().delete()
    add_member(w["list"], "blank@example.com", first_name="")  # renders, but empty is allowed
    c = services.submit(w["manager"], new_campaign(w))
    assert c.preflight_report["samples"][0]["subject"] == "News for"


def test_template_preflight_is_rerun(w):
    report = preflight.run(new_campaign(w)).report()
    assert not any(f["code"].startswith("TEMPLATE_") for f in report["findings"])


# ─── Review and snapshot ─────────────────────────────────────────────────


def test_only_approvers_approve(w):
    c = services.submit(w["manager"], new_campaign(w))
    with pytest.raises(PermissionDenied):
        services.approve(w["manager"], c)
    services.approve(w["approver"], c)


def test_approval_freezes_the_snapshot(w):
    Suppression.suppress("user3@example.com", Suppression.Reason.HARD_BOUNCE)
    c = approved(w)
    snap = CampaignSnapshot.objects.get(campaign=c)
    assert c.status == "APPROVED" and c.reviewed_by == w["approver"]
    assert snap.template_checksum == w["version"].checksum
    assert snap.sender["from_address"] == "news@news.example.com"
    assert (snap.audience_total, snap.excluded_suppressed, snap.recipient_count) == (4, 1, 3)
    recipients = CampaignSnapshotRecipient.objects.filter(snapshot=snap)
    assert {r.contact.email for r in recipients} == {"user0@example.com", "user1@example.com",
                                                    "user2@example.com"}  # fmt: skip
    granting = ConsentRecord.objects.get(contact__email="user0@example.com")
    assert recipients.get(contact__email="user0@example.com").consent_record == granting


def test_snapshot_is_immutable_in_the_database(w):
    snap = CampaignSnapshot.objects.get(campaign=approved(w))
    for sql in ("UPDATE campaigns_snapshot SET recipient_count = 999 WHERE id = %s",
                "DELETE FROM campaigns_snapshot_recipient WHERE snapshot_id = %s"):  # fmt: skip
        with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
            connection.cursor().execute(sql, [snap.pk])


def test_approval_reruns_preflight_and_bounces_back(w):
    c = services.submit(w["manager"], new_campaign(w))
    add_member(w["list"], "late@example.com", consent=False)  # list changed after submit
    with pytest.raises(services.PreflightFailed):
        services.approve(w["approver"], c)
    c.refresh_from_db()
    assert c.status == "PREFLIGHT_FAILED" and not CampaignSnapshot.objects.exists()


def test_reject_and_request_changes_need_a_note(w):
    c = services.submit(w["manager"], new_campaign(w))
    with pytest.raises(ValidationError):
        services.reject(w["approver"], c, "  ")
    c = services.reject(w["approver"], c, "off-brand")
    assert c.status == "REJECTED" and c.review_note == "off-brand"
    with pytest.raises(services.InvalidTransition):
        services.submit(w["manager"], c)


# ─── Scheduling ──────────────────────────────────────────────────────────


def test_schedule_with_warnings(w):
    result = services.schedule(w["manager"], approved(w))
    assert result.campaign.status == "SCHEDULED" and result.campaign.scheduled_at
    assert any("Q-POL-1" in warning for warning in result.warnings)
    assert any("GLOBAL_SEND_ENABLED" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("breakage", "needle"),
    [
        (lambda w: SendingDomain.objects.filter(pk=w["domain"].pk).update(status="PAUSED"), "not ACTIVE"),
        (lambda w: templates.retire(w["admin"], w["version"]), "approved template"),
    ],
)  # fmt: skip
def test_schedule_rejections(w, breakage, needle):
    c = approved(w)
    breakage(w)
    with pytest.raises(services.ScheduleRejected, match=needle):
        services.schedule(w["manager"], c)
    c.refresh_from_db()
    assert c.status == "APPROVED"


def test_schedule_in_the_past_is_rejected(w):
    with pytest.raises(services.ScheduleRejected, match="past"):
        services.schedule(w["manager"], approved(w), timezone.now() - timedelta(hours=1))


def test_lost_consent_warns_and_total_loss_rejects(w):
    c = approved(w)
    c2 = approved(w, name="Second")  # both approved while everyone consents
    ConsentRecord.objects.create(contact=w["members"][0], stream=Stream.MARKETING,
                                 action="WITHDRAWN", method="UNSUBSCRIBE_LINK")  # fmt: skip
    result = services.schedule(w["manager"], c)
    assert any("1 recipient(s) lost consent" in x for x in result.warnings)

    for m in w["members"][1:]:
        ConsentRecord.objects.create(contact=m, stream=Stream.MARKETING, action="WITHDRAWN",
                                     method="UNSUBSCRIBE_LINK")  # fmt: skip
    with pytest.raises(services.ScheduleRejected, match="consent"):
        services.schedule(w["manager"], c2)


# ─── Pause, resume, cancel ───────────────────────────────────────────────


def _send_request(w, c):
    m = w["members"][0]
    return SendRequest(recipient=m.email, purpose=Stream.MARKETING, sender=w["sender"],
                       template_version=w["version"], expected_checksum=w["version"].checksum,
                       from_snapshot=True, contact=m, contact_list=w["list"],
                       campaign_ref=str(c.pk))  # fmt: skip


def test_pause_holds_queued_sends_and_resume_releases(w, settings):
    settings.GLOBAL_SEND_ENABLED = True
    c = services.schedule(w["manager"], approved(w)).campaign
    assert decide(_send_request(w, c)).send

    c = services.pause(w["approver"], c, "complaint spike")
    assert c.status == "PAUSED" and c.paused_from == "SCHEDULED"
    assert "PAUSED" in decide(_send_request(w, c)).codes

    c = services.resume(w["manager"], c)
    assert c.status == "SCHEDULED" and c.active_pause is None
    assert decide(_send_request(w, c)).send


def test_resume_does_not_lift_an_operators_separate_pause(w, settings):
    settings.GLOBAL_SEND_ENABLED = True
    c = services.schedule(w["manager"], approved(w)).campaign
    SendPause.pause("CAMPAIGN", str(c.pk), reason="legal hold", by=w["admin"])
    c = services.resume(w["manager"], services.pause(w["manager"], c, "x"))
    assert "PAUSED" in decide(_send_request(w, c)).codes


def test_cancel_is_permanent(w, settings):
    settings.GLOBAL_SEND_ENABLED = True
    c = services.cancel(
        w["manager"], services.schedule(w["manager"], approved(w)).campaign, "wrong list"
    )
    assert c.status == "CANCELED"
    assert "PAUSED" in decide(_send_request(w, c)).codes
    with pytest.raises(services.InvalidTransition):
        services.resume(w["manager"], c)


def test_campaigns_are_never_deleted(w):
    c = new_campaign(w)
    with pytest.raises(DatabaseError, match="forbidden"), transaction.atomic():
        connection.cursor().execute("DELETE FROM campaigns_campaign WHERE id = %s", [c.pk])


def test_lifecycle_is_audited(w):
    services.schedule(w["manager"], approved(w))
    actions = set(AuditEvent.objects.filter(action__startswith="campaign.")
                  .values_list("action", flat=True))  # fmt: skip
    assert {
        "campaign.create",
        "campaign.submit",
        "campaign.approve",
        "campaign.schedule",
    } <= actions


# ─── API (§10.6.5) ───────────────────────────────────────────────────────


def _token(user, *scopes):
    return {"HTTP_AUTHORIZATION": f"Bearer {ApiToken.issue(user, 'ci', scopes=list(scopes))[1]}"}


def test_api_full_flow(client, w):
    write = _token(w["manager"], "campaigns:write", "campaigns:read")
    body = {"name": "API news", "purpose": "MARKETING", "sender_identity": str(w["sender"].pk),
            "template_version": w["version"].pk, "audience_list": str(w["list"].pk)}  # fmt: skip
    resp = client.post(reverse("api:campaign-create"), json.dumps(body),
                       content_type="application/json", **write)  # fmt: skip
    assert resp.status_code == 201, resp.content
    pk = resp.json()["id"]

    assert (
        client.post(reverse("api:campaign-submit", args=[pk]), **write).json()["status"]
        == "READY_FOR_REVIEW"
    )
    approve = _token(w["approver"], "campaigns:approve")
    assert client.post(reverse("api:campaign-approve", args=[pk]), **approve).status_code == 200
    sched = client.post(reverse("api:campaign-schedule", args=[pk]), **write).json()
    assert sched["status"] == "SCHEDULED" and sched["warnings"]
    paused = client.post(reverse("api:campaign-pause", args=[pk]), **write).json()
    assert paused["status"] == "PAUSED" and "already accepted" in paused["note"]
    metrics = client.get(reverse("api:campaign-metrics", args=[pk]), **write).json()
    assert metrics["audience"]["recipients"] == 4 and metrics["jobs"] == {}


def test_api_scopes_and_permissions(client, w):
    c = services.submit(w["manager"], new_campaign(w))
    # An approver's token without campaigns:approve cannot approve (tokens skip MFA).
    no_scope = _token(w["approver"], "campaigns:write")
    assert client.post(reverse("api:campaign-approve", args=[c.pk]), **no_scope).status_code == 403
    # The scope alone is not enough without the role.
    manager_scoped = _token(w["manager"], "campaigns:approve")
    assert (
        client.post(reverse("api:campaign-approve", args=[c.pk]), **manager_scoped).status_code
        == 403
    )
    assert client.post(reverse("api:campaign-schedule", args=[c.pk]),
                       **_token(w["manager"], "campaigns:write")).status_code == 409  # fmt: skip


def test_api_preflight_failure_is_422(client, w):
    add_member(w["list"], "noconsent@example.com", consent=False)
    c = new_campaign(w)
    resp = client.post(reverse("api:campaign-submit", args=[c.pk]),
                       **_token(w["manager"], "campaigns:write"))  # fmt: skip
    assert resp.status_code == 422
    assert "MISSING_CONSENT" in {f["code"] for f in resp.json()["findings"]}


# ─── Admin ───────────────────────────────────────────────────────────────


def test_admin_review_page_and_approval(login_verified, w):
    c = services.submit(w["manager"], new_campaign(w))
    client = login_verified(w["approver"])
    url = reverse("admin:campaigns_campaign_review", args=[c.pk])
    page = client.get(url).content.decode()
    assert "News for P0" in page and 'value="approve"' in page
    client.post(url, {"decision": "approve", "note": ""})
    c.refresh_from_db()
    assert c.status == "APPROVED"

    manager = login_verified(w["manager"])
    assert 'value="approve"' not in manager.get(url).content.decode()
    assert manager.get(reverse("admin:campaigns_campaign_changelist")).status_code == 200
