"""Operations console (docs/design/console-mockup.html): access, every page, and the
main actions driven through the console's own forms — same services, same audit."""

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from campaigns import services as campaign_services
from campaigns.models import Campaign
from common.models import Stream
from consent.models import ConsentRecord
from contacts.models import Contact, ContactList, ListMembership
from delivery.models import SendPause
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as template_services
from email_templates.models import EmailTemplate, TemplateVersion
from suppression.models import Suppression

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

HTML = '<p>Hi {{ first_name }}</p><p><a href="{{ unsubscribe_url }}">Unsubscribe</a></p>'
TEXT = "Hi {{ first_name }}\nUnsubscribe: {{ unsubscribe_url }}\n"
PAGES = ["today", "campaigns", "campaign_new", "templates", "template_new", "approvals",
         "audience", "domains", "events", "suppressions", "controls", "audit", "unsubscribe"]  # fmt: skip


@pytest.fixture
def people(django_user_model):
    def make(email, code):
        user = django_user_model.objects.create_user(email, "pw", is_staff=True)
        UserRole.grant(user, code, by=None)
        return django_user_model.objects.get(pk=user.pk)

    return {
        "admin": make("admin@example.com", Role.Code.SYSTEM_ADMIN),
        "manager": make("manager@example.com", Role.Code.CAMPAIGN_MANAGER),
        "approver": make("approver@example.com", Role.Code.APPROVER),
        "analyst": make("analyst@example.com", Role.Code.ANALYST),
    }


@pytest.fixture
def world(people):
    now = timezone.now()
    domain = SendingDomain.objects.create(
        domain="news.example.com", stream=Stream.MARKETING, return_path_domain="b.example.com",
        ip_pool=IpPool.objects.create(name="mkt", stream=Stream.MARKETING), status="ACTIVE",
        spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now,
    )  # fmt: skip
    sender = SenderIdentity.objects.create(domain=domain, local_part="news", display_name="News")
    lst = ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)
    for i in range(3):
        c = Contact.objects.create(email=f"u{i}@example.net", first_name=f"P{i}")
        ListMembership.objects.create(contact=c, contact_list=lst)
        ConsentRecord.objects.create(
            contact=c, stream=Stream.MARKETING, action="GRANTED", method="WEB_FORM"
        )
    _, v = template_services.create_template(
        people["admin"], name="Newsletter", category="marketing", subject="Hi {{ first_name }}",
        html_body=HTML, text_body=TEXT, variables={"first_name": {"required": True}},
        sample_data={"first_name": "Ada"},
    )  # fmt: skip
    template_services.submit(people["admin"], v)
    v = template_services.approve(people["admin"], v)
    return {**people, "domain": domain, "sender": sender, "list": lst, "version": v}


@pytest.fixture
def as_(login_verified):
    return login_verified  # login_verified(user) → client with MFA satisfied


# ─── Access ──────────────────────────────────────────────────────────────


def test_anonymous_is_sent_to_the_console_sign_in(client):
    resp = client.get(reverse("console:today"))
    assert resp.status_code == 302 and resp["Location"].startswith(reverse("console:login"))
    page = client.get(reverse("console:login")).content.decode()
    assert "Sign in" in page and "[MOCK]" in page


def test_signing_in_lands_on_today(client, people):
    resp = client.post(
        reverse("console:login"), {"username": "manager@example.com", "password": "pw"}
    )
    assert resp.status_code == 302 and resp["Location"] == reverse("console:today")


def test_a_user_without_a_role_gets_no_access(as_, django_user_model):
    nobody = django_user_model.objects.create_user("nobody@example.com", "pw")
    resp = as_(nobody).get(reverse("console:today"))
    assert resp.status_code == 403 and "don't have a role" in resp.content.decode()


@pytest.mark.parametrize("name", PAGES)
def test_every_page_renders_with_the_safety_bar_and_send_state(as_, world, name):
    page = as_(world["admin"]).get(reverse(f"console:{name}"))
    assert page.status_code == 200, name
    body = page.content.decode()
    assert "[MOCK]" in body and "Sending disabled" in body  # GLOBAL_SEND_ENABLED=false in tests


def test_detail_pages_render(as_, world):
    client = as_(world["admin"])
    c = campaign_services.create(world["manager"], name="Oct", purpose="MARKETING",
                                 sender_identity=world["sender"], template_version=world["version"],
                                 audience_list=world["list"])  # fmt: skip
    for url in (reverse("console:campaign", args=[c.pk]), reverse("console:campaign_edit", args=[c.pk]),
                reverse("console:template_review", args=[world["version"].pk])):  # fmt: skip
        assert client.get(url).status_code == 200, url


def test_analyst_sees_but_cannot_act(as_, world):
    client = as_(world["analyst"])
    body = client.get(reverse("console:campaigns")).content.decode()
    assert 'href="/campaigns/new/"' not in body and "Needs the Campaign Manager role" in body
    client.post(reverse("console:suppressions"),
                {"target": "x@example.com", "scope": "GLOBAL", "reason": "MANUAL", "source": "t"})  # fmt: skip
    assert not Suppression.objects.exists()
    client.post(reverse("console:controls"), {"action": "pause", "scope": "GLOBAL", "reason": "x"})
    assert not SendPause.objects.exists()


def test_static_css_is_served(client):
    for path in (
        "/static/console/console.css",
        "/static/console/tokens.css",
        "/static/admin/css/base.css",
    ):
        assert client.get(path).status_code == 200, path


# ─── Campaign workflow through the console ───────────────────────────────


def test_campaign_create_submit_approve_schedule_pause(as_, world):
    manager, approver = as_(world["manager"]), None
    resp = manager.post(reverse("console:campaign_new"), {
        "name": "Console campaign", "purpose": "MARKETING", "sender_identity": world["sender"].pk,
        "template_version": world["version"].pk, "audience_list": world["list"].pk,
    })  # fmt: skip
    c = Campaign.objects.get(name="Console campaign")
    url = reverse("console:campaign", args=[c.pk])
    assert resp["Location"] == url

    manager.post(url, {"action": "submit"})
    c.refresh_from_db()
    assert c.status == "READY_FOR_REVIEW"
    assert "Eligible" in manager.get(url).content.decode()  # audience funnel

    manager.post(url, {"action": "approve"})  # managers cannot approve
    c.refresh_from_db()
    assert c.status == "READY_FOR_REVIEW"

    approver = as_(world["approver"])
    approver.post(url, {"action": "approve", "note": ""})
    c.refresh_from_db()
    assert c.status == "APPROVED"

    manager = as_(world["manager"])
    manager.post(url, {"action": "schedule"})
    c.refresh_from_db()
    assert c.status == "SCHEDULED"
    manager.post(url, {"action": "pause", "note": "complaints"})
    c.refresh_from_db()
    assert c.status == "PAUSED"
    assert AuditEvent.objects.filter(action="campaign.pause", actor=world["manager"]).exists()


def test_a_bad_schedule_date_never_means_now(as_, world):
    """Regression: an unparseable start time must not fall through to 'send now'."""
    c = campaign_services.approve(world["approver"], campaign_services.submit(
        world["manager"], campaign_services.create(
            world["manager"], name="Oct", purpose="MARKETING", sender_identity=world["sender"],
            template_version=world["version"], audience_list=world["list"])))  # fmt: skip
    client = as_(world["manager"])
    resp = client.post(reverse("console:campaign", args=[c.pk]),
                       {"action": "schedule", "scheduled_at": "not-a-date"}, follow=True)  # fmt: skip
    c.refresh_from_db()
    assert c.status == "APPROVED" and "Nothing was scheduled" in resp.content.decode()


def test_refusals_are_shown_not_crashed(as_, world):
    c = campaign_services.create(world["manager"], name="Oct", purpose="MARKETING",
                                 sender_identity=world["sender"], template_version=world["version"],
                                 audience_list=world["list"])  # fmt: skip
    resp = as_(world["manager"]).post(reverse("console:campaign", args=[c.pk]),
                                      {"action": "schedule"}, follow=True)  # fmt: skip
    assert resp.status_code == 200 and "expected APPROVED" in resp.content.decode()


# ─── Templates through the console ───────────────────────────────────────


def test_template_new_edit_submit_review(as_, world):
    manager = as_(world["manager"])
    resp = manager.post(reverse("console:template_new"), {
        "name": "Receipt", "category": "transactional", "description": "",
        "subject": "Receipt {{ order_id }}", "html_body": "<p>Order {{ order_id }}</p>",
        "text_body": "Order {{ order_id }}", "variables": '{"order_id": {"required": true}}',
        "sample_data": '{"order_id": "A-1"}',
    })  # fmt: skip
    v = TemplateVersion.objects.get(template__name="Receipt")
    editor = reverse("console:template_version", args=[v.pk])
    assert resp["Location"] == editor

    bad = manager.post(editor, {"action": "save", "subject": "x", "html_body": "<p>x</p>",
                                "text_body": "x", "variables": "{not json", "sample_data": "{}"})  # fmt: skip
    assert "not valid JSON" in bad.content.decode()

    manager.post(editor, {"action": "save", "subject": "Receipt for {{ order_id }}",
                          "html_body": "<p>Order {{ order_id }}</p>", "text_body": "Order {{ order_id }}",
                          "variables": '{"order_id": {"required": true}}', "sample_data": '{"order_id": "A-1"}'})  # fmt: skip
    v.refresh_from_db()
    assert v.subject == "Receipt for {{ order_id }}"

    manager.post(editor, {"action": "submit"})
    v.refresh_from_db()
    assert v.status == "PENDING_APPROVAL"
    assert manager.get(editor)["Location"] == reverse("console:template_review", args=[v.pk])

    review = reverse("console:template_review", args=[v.pk])
    page = as_(world["approver"]).get(review).content.decode()
    assert 'sandbox=""' in page and "Receipt for A-1" in page
    as_(world["approver"]).post(review, {"action": "approve", "note": ""})
    v.refresh_from_db()
    assert v.status == "APPROVED"


def test_review_page_escapes_template_html(as_, world):
    _, v = template_services.create_template(
        world["admin"], name="Bad", category="transactional", subject="x",
        html_body="<p>x</p><script>alert(1)</script>", text_body="x")  # fmt: skip
    page = as_(world["admin"]).get(reverse("console:template_review", args=[v.pk])).content.decode()
    assert "<script>alert(1)</script>" not in page and "UNSAFE_ELEMENT" in page


def test_template_list_shows_live_and_in_progress(as_, world):
    template_services.new_version_from(world["admin"], world["version"])
    body = as_(world["analyst"]).get(reverse("console:templates")).content.decode()
    assert "v1" in body and "v2" in body
    assert EmailTemplate.objects.count() == 1


# ─── Deliverability ──────────────────────────────────────────────────────


def test_add_and_look_up_suppressions(as_, world):
    client = as_(world["admin"])
    client.post(reverse("console:suppressions"),
                {"target": "Bob@Example.com", "scope": "MARKETING", "reason": "UNSUBSCRIBE", "source": "phone"})  # fmt: skip
    client.post(reverse("console:suppressions"),
                {"target": "*@spam.example", "scope": "GLOBAL", "reason": "MANUAL", "source": "legal"})  # fmt: skip
    assert Suppression.objects.get(domain="spam.example").scope == "GLOBAL"
    assert Suppression.objects.get(email="bob@example.com").stream == "MARKETING"
    assert (
        "*@spam.example" in client.get(reverse("console:suppressions") + "?q=spam").content.decode()
    )
    miss = client.get(reverse("console:suppressions") + "?q=nobody@example.org").content.decode()
    assert "Not suppressed" in miss


def test_pause_and_resume_all_sending(as_, world, settings):
    settings.GLOBAL_SEND_ENABLED = True
    client = as_(world["admin"])
    assert "All sending running" in client.get(reverse("console:today")).content.decode()
    client.post(reverse("console:controls"),
                {"action": "pause", "scope": "GLOBAL", "target": "", "reason": "incident"})  # fmt: skip
    pause = SendPause.objects.active().get(scope="GLOBAL")
    assert pause.reason == "incident" and pause.created_by == world["admin"]
    assert "All sending paused" in client.get(reverse("console:today")).content.decode()
    client.post(reverse("console:controls"), {"action": "resume", "pause": pause.pk})
    assert not SendPause.objects.active().exists()


def test_domain_recheck_stores_and_shows_the_report(as_, world, monkeypatch, resolver):
    from workers.services import dns_checks

    monkeypatch.setattr(dns_checks, "dnspython_resolver", lambda: resolver)
    d = SendingDomain.objects.create(domain="mail.example.com", stream=Stream.MARKETING,
                                     return_path_domain="bounce.example.com", dkim_selector="s1")  # fmt: skip
    client = as_(world["admin"])
    client.post(reverse("console:domains"), {"action": "recheck", "domain": d.pk})
    d.refresh_from_db()
    assert {r["check"] for r in d.last_dns_report} >= {"spf", "dkim", "dmarc"}
    page = client.get(reverse("console:domains") + f"?d={d.pk}").content.decode()
    assert "DKIM" in page and "v=DMARC1" in page


def test_today_counts_real_activity(as_, world):
    Suppression.suppress("u0@example.net", Suppression.Reason.UNSUBSCRIBE,
                         scope=Suppression.Scope.STREAM, stream=Stream.MARKETING)  # fmt: skip
    body = as_(world["admin"]).get(reverse("console:today")).content.decode()
    assert "No rate limits configured" in body and "Q-POL-1" in body
    assert "Complaints" in body and "Hard bounced" in body


def test_audit_log_filters(as_, world):
    client = as_(world["admin"])
    body = client.get(reverse("console:audit") + "?obj=email_templates.").content.decode()
    assert "email_template.version.approve" in body
    assert (
        "email_template.version.approve"
        not in client.get(reverse("console:audit") + "?obj=domains.").content.decode()
    )


def test_unsubscribe_page_uses_the_shared_card(client):
    from suppression import unsubscribe

    c = Contact.objects.create(email="bob@example.com")
    page = client.get(reverse("unsubscribe", args=[unsubscribe.make_token(c.pk)])).content.decode()
    assert "Stop marketing email?" in page and "console.css" in page
