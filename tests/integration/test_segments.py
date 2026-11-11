"""Segments (provisional rule language, Q-DATA-1): evaluation, validation, and
use as a campaign audience — a segment narrows a list but never bypasses
consent or suppression."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from campaigns import services as campaigns
from campaigns.models import CampaignSnapshot
from common.models import Stream
from consent.models import ConsentRecord
from contacts import segments
from contacts import services as contact_services
from contacts.models import Contact, ContactList, ListMembership, Segment
from domains.models import IpPool, SenderIdentity, SendingDomain
from email_templates import services as templates
from suppression.models import Suppression

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


@pytest.fixture
def lst():
    return ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)


def member(lst, email, *, first="", last="", consent=True, **attributes):
    c = Contact.objects.create(email=email, first_name=first, last_name=last, attributes=attributes)
    ListMembership.objects.create(contact=c, contact_list=lst)
    if consent:
        ConsentRecord.objects.create(contact=c, stream=Stream.MARKETING, action="GRANTED",
                                     method="WEB_FORM")  # fmt: skip
    return c


@pytest.fixture
def people(lst):
    return {
        "ada": member(lst, "ada@gmail.com", first="Ada", plan="pro", seats=12),
        "bob": member(lst, "bob@corp.example", first="Bob", plan="free", seats=1),
        "cy": member(lst, "cy@corp.example", first="Cy", plan="PRO"),
        "dee": member(lst, "dee@gmail.com", first=""),
    }


def selected(lst, definition):
    seg = Segment(contact_list=lst, name="t", definition=definition)
    return sorted(c.email.partition("@")[0] for c in seg.members())


# ─── Evaluation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("definition", "expected"), [
    ({}, ["ada", "bob", "cy", "dee"]),
    ({"field": "email_domain", "op": "eq", "value": "GMAIL.com"}, ["ada", "dee"]),
    ({"field": "email_domain", "op": "not_in", "value": ["gmail.com"]}, ["bob", "cy"]),
    ({"field": "first_name", "op": "in", "value": ["ada", "BOB"]}, ["ada", "bob"]),
    ({"field": "first_name", "op": "exists", "value": False}, ["dee"]),
    ({"field": "first_name", "op": "starts_with", "value": "c"}, ["cy"]),
    ({"field": "attributes.plan", "op": "eq", "value": "pro"}, ["ada", "cy"]),
    ({"field": "attributes.plan", "op": "ne", "value": "pro"}, ["bob", "dee"]),  # missing counts
    ({"field": "attributes.plan", "op": "exists", "value": True}, ["ada", "bob", "cy"]),
    ({"field": "attributes.seats", "op": "gte", "value": 10}, ["ada"]),
    ({"field": "attributes.seats", "op": "lt", "value": 10}, ["bob"]),
    ({"all": [{"field": "email_domain", "op": "eq", "value": "corp.example"},
              {"field": "attributes.plan", "op": "eq", "value": "pro"}]}, ["cy"]),
    ({"any": [{"field": "first_name", "op": "eq", "value": "dee"},
              {"field": "attributes.seats", "op": "gt", "value": 5}]}, ["ada"]),
    ({"not": {"field": "email_domain", "op": "eq", "value": "gmail.com"}}, ["bob", "cy"]),
])  # fmt: skip
def test_rules_select_the_right_members(lst, people, definition, expected):
    assert selected(lst, definition) == expected


def test_a_segment_never_reaches_beyond_its_list(lst, people):
    other = ContactList.objects.create(name="Other", stream=Stream.MARKETING)
    member(other, "outsider@gmail.com")
    assert "outsider" not in selected(
        lst, {"field": "email_domain", "op": "eq", "value": "gmail.com"}
    )


def test_list_and_time_conditions(lst, people):
    vip = ContactList.objects.create(name="VIP", stream=Stream.MARKETING)
    ListMembership.objects.create(contact=people["bob"], contact_list=vip)
    assert selected(lst, {"field": "list", "op": "in", "value": [str(vip.pk)]}) == ["bob"]
    assert "bob" not in selected(lst, {"field": "list", "op": "not_in", "value": [str(vip.pk)]})

    old = timezone.now() - timedelta(days=400)
    Contact.objects.filter(pk=people["ada"].pk).update(created_at=old)
    cutoff = (timezone.now() - timedelta(days=30)).date().isoformat()
    assert selected(lst, {"field": "created_at", "op": "before", "value": cutoff}) == ["ada"]
    ConsentRecord.objects.create(contact=people["dee"], stream=Stream.MARKETING,
                                 action="GRANTED", method="WEB_FORM", occurred_at=old)  # fmt: skip
    assert selected(lst, {"field": "consent_granted_at", "op": "before", "value": cutoff}) == [
        "dee"
    ]


@pytest.mark.parametrize(("definition", "message"), [
    ([], "must be a JSON object"),
    ({"field": "email", "op": "eq", "value": "x"}, "unknown field 'email'"),
    ({"field": "email_domain", "op": "contains", "value": "x"}, "does not support 'contains'"),
    ({"field": "attributes.bad key", "op": "eq", "value": "x"}, "attribute name"),
    ({"field": "attributes.n", "op": "gt", "value": "5"}, "needs a number"),
    ({"field": "first_name", "op": "in", "value": []}, "non-empty list"),
    ({"field": "first_name", "op": "exists", "value": "yes"}, "true or false"),
    ({"field": "created_at", "op": "before", "value": "soon"}, "ISO date"),
    ({"field": "list", "op": "in", "value": ["00000000-0000-0000-0000-000000000000"]}, "no such list"),
    ({"all": []}, "non-empty list"),
    ({"all": [{"field": "first_name", "op": "eq", "value": "a"}], "any": []}, "expected"),
    ({"not": {"not": {"not": {"not": {"not": {"not": {"field": "first_name", "op": "eq",
                                                      "value": "a"}}}}}}}, "deeper"),
    ({"any": [{"field": "first_name", "op": "eq", "value": "a"}] * 51}, "more than 50"),
])  # fmt: skip
def test_invalid_rules_name_the_problem_and_where(definition, message):
    with pytest.raises(ValidationError, match=message):
        segments.validate(definition)


def test_errors_carry_a_path(lst):
    with pytest.raises(ValidationError, match=r"\$\.all\[1\]"):
        segments.validate({"all": [{"field": "first_name", "op": "eq", "value": "a"},
                                   {"field": "nope", "op": "eq", "value": 1}]})  # fmt: skip


# ─── Authoring ───────────────────────────────────────────────────────────


def test_create_and_edit_are_permission_checked_validated_and_audited(person, lst):
    manager = person("m@example.com", Role.Code.CAMPAIGN_MANAGER)
    analyst = person("a@example.com", Role.Code.ANALYST)
    rules = {"field": "email_domain", "op": "eq", "value": "gmail.com"}
    with pytest.raises(PermissionDenied):
        contact_services.create_segment(analyst, contact_list=lst, name="Gmail", definition=rules)
    with pytest.raises(ValidationError):
        contact_services.create_segment(manager, contact_list=lst, name="Bad",
                                        definition={"field": "x", "op": "eq", "value": 1})  # fmt: skip
    seg = contact_services.create_segment(manager, contact_list=lst, name="Gmail", definition=rules)
    with pytest.raises(ValidationError):  # name is unique per list
        contact_services.create_segment(manager, contact_list=lst, name="Gmail", definition={})
    contact_services.update_segment(manager, seg, definition={})
    actions = list(
        AuditEvent.objects.filter(target_id=str(seg.pk)).values_list("action", flat=True)
    )
    assert sorted(actions) == ["segment.create", "segment.edit"]


def test_console_segment_pages(person, lst, people, login_verified):
    client = login_verified(person("m@example.com", Role.Code.CAMPAIGN_MANAGER))
    url = reverse("console:segment_new") + f"?list={lst.pk}"
    assert client.get(url).status_code == 200
    response = client.post(url, {"list": lst.pk, "name": "Gmail",
                                 "definition": '{"field": "email_domain", "op": "eq", "value": "gmail.com"}'})  # fmt: skip
    seg = Segment.objects.get(name="Gmail")
    assert response.status_code == 302
    page = client.get(reverse("console:segment", args=[seg.pk])).content.decode()
    assert "of 4 list members" in page
    bad = client.post(reverse("console:segment", args=[seg.pk]),
                      {"name": "Gmail", "definition": '{"field": "nope"}'})  # fmt: skip
    assert bad.status_code == 200 and "Segment rule at" in bad.content.decode()
    assert "Gmail" in client.get(reverse("console:audience")).content.decode()


def test_analyst_can_view_but_not_edit_a_segment(person, lst, login_verified):
    seg = Segment.objects.create(contact_list=lst, name="All", definition={})
    client = login_verified(person("a@example.com", Role.Code.ANALYST))
    assert client.get(reverse("console:segment", args=[seg.pk])).status_code == 200
    assert client.post(reverse("console:segment", args=[seg.pk]),
                       {"name": "x", "definition": "{}"}).status_code == 403  # fmt: skip
    assert client.get(reverse("console:segment_new") + f"?list={lst.pk}").status_code == 403


# ─── Campaign audiences ──────────────────────────────────────────────────


@pytest.fixture
def campaign_world(person, lst, people):
    admin = person("admin@example.com", Role.Code.SYSTEM_ADMIN)
    now = timezone.now()
    domain = SendingDomain.objects.create(
        domain="news.example.com", stream=Stream.MARKETING, return_path_domain="b.example.com",
        ip_pool=IpPool.objects.create(name="mkt", stream=Stream.MARKETING), status="ACTIVE",
        spf_verified_at=now, dkim_verified_at=now, dmarc_verified_at=now,
    )  # fmt: skip
    _, v = templates.create_template(
        admin, name="News", category="marketing", subject="Hi {{ first_name }}",
        html_body=HTML, text_body=TEXT, variables={"first_name": {"required": False, "default": "there"}},
        sample_data={"first_name": "Ada"},
    )  # fmt: skip
    templates.submit(admin, v)
    gmail = Segment.objects.create(contact_list=lst, name="Gmail",
                                   definition={"field": "email_domain", "op": "eq", "value": "gmail.com"})  # fmt: skip
    return {
        "admin": admin,
        "sender": SenderIdentity.objects.create(domain=domain, local_part="news", display_name="N"),
        "version": templates.approve(admin, v),
        "segment": gmail,
    }


def make_campaign(w, lst, **extra):
    return campaigns.create(w["admin"], name="C", purpose=Stream.MARKETING,
                            sender_identity=w["sender"], template_version=w["version"],
                            audience_list=lst, **extra)  # fmt: skip


def test_segment_narrows_the_audience_and_is_frozen_in_the_snapshot(campaign_world, lst, people):
    w = campaign_world
    c = campaigns.approve(w["admin"], campaigns.submit(w["admin"],
                                                       make_campaign(w, lst, audience_segment=w["segment"])))  # fmt: skip
    snapshot = CampaignSnapshot.objects.get(campaign=c)
    assert snapshot.recipient_count == 2 and snapshot.segment == w["segment"]
    assert snapshot.segment_definition == w["segment"].definition
    assert {r.contact_id for r in snapshot.recipients.all()} == {people["ada"].pk, people["dee"].pk}
    assert "SEGMENT_APPLIED" in {f["code"] for f in c.preflight_report["findings"]}

    # Editing the segment later does not change what was approved.
    contact_services.update_segment(w["admin"], w["segment"], definition={})
    snapshot.refresh_from_db()
    assert snapshot.segment_definition == {
        "field": "email_domain",
        "op": "eq",
        "value": "gmail.com",
    }


def test_segment_does_not_bypass_consent_or_suppression(campaign_world, lst, people):
    w = campaign_world
    Suppression.suppress("ada@gmail.com", Suppression.Reason.COMPLAINT)
    member(lst, "noconsent@gmail.com", consent=False)
    c = make_campaign(w, lst, audience_segment=w["segment"])
    with pytest.raises(campaigns.PreflightFailed):
        campaigns.submit(w["admin"], c)
    c.refresh_from_db()
    found = {f["code"] for f in c.preflight_report["findings"]}
    assert {"MISSING_CONSENT", "SUPPRESSED_EXCLUDED"} <= found


def test_segment_must_belong_to_the_campaign_list(campaign_world, lst):
    w = campaign_world
    other = ContactList.objects.create(name="Other", stream=Stream.MARKETING)
    with pytest.raises(ValidationError, match="not a segment of"):
        make_campaign(w, other, audience_segment=w["segment"])
    c = make_campaign(w, lst, audience_segment=w["segment"])
    with pytest.raises(ValidationError, match="not a segment of"):
        campaigns.update(w["admin"], c, audience_list=other)


def test_invalid_segment_rules_fail_preflight(campaign_world, lst):
    w = campaign_world
    c = make_campaign(w, lst, audience_segment=w["segment"])
    Segment.objects.filter(pk=w["segment"].pk).update(definition={"field": "bogus"})
    with pytest.raises(campaigns.PreflightFailed):
        campaigns.submit(w["admin"], c)
    c.refresh_from_db()
    assert "SEGMENT_INVALID" in {f["code"] for f in c.preflight_report["findings"]}


def test_api_accepts_and_returns_a_segment(campaign_world, lst, client):
    from accounts.models import ApiToken

    w = campaign_world
    _, secret = ApiToken.issue(w["admin"], "t", scopes=["campaigns:write", "campaigns:read"])
    body = {"name": "API", "purpose": "MARKETING", "sender_identity": str(w["sender"].pk),
            "template_version": w["version"].pk, "audience_list": str(lst.pk),
            "audience_segment": str(w["segment"].pk)}  # fmt: skip
    response = client.post("/api/v1/campaigns", body, content_type="application/json",
                           HTTP_AUTHORIZATION=f"Bearer {secret}")  # fmt: skip
    assert response.status_code == 201, response.content
    assert response.json()["audience_segment"] == str(w["segment"].pk)
