"""consent.evaluation: the bulk query (campaign preflight) and the single lookup
(policy service) must agree on every history, including ties and expiry."""

from datetime import timedelta

import pytest
from django.utils import timezone

from common.models import Stream
from consent.evaluation import (
    CONSENT_EXPIRED,
    CONSENT_WITHDRAWN,
    GRANTED,
    NO_CONSENT,
    marketing_consent,
    marketing_consent_bulk,
)
from consent.models import ConsentRecord
from contacts.models import Contact, ContactList

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

NOW = timezone.now()


def record(contact, action, *, days_ago=0, lst=None, at=None):
    return ConsentRecord.objects.create(
        contact=contact, stream=Stream.MARKETING, contact_list=lst, action=action,
        method="WEB_FORM", occurred_at=at or NOW - timedelta(days=days_ago),
    )  # fmt: skip


@pytest.fixture
def histories(settings):
    settings.CONSENT_MAX_AGE_DAYS = 365
    lst = ContactList.objects.create(name="L", stream=Stream.MARKETING)
    c = {name: Contact.objects.create(email=f"{name}@example.com") for name in
         ("granted", "list_only", "withdrawn", "regranted", "list_withdrawn", "expired",
          "tie", "nothing", "other_list")}  # fmt: skip
    record(c["granted"], "GRANTED")
    record(c["list_only"], "GRANTED", lst=lst)
    record(c["withdrawn"], "GRANTED", days_ago=5)
    record(c["withdrawn"], "WITHDRAWN", days_ago=1)
    record(c["regranted"], "WITHDRAWN", days_ago=5)
    record(c["regranted"], "GRANTED", days_ago=1)
    record(c["list_withdrawn"], "GRANTED")
    record(c["list_withdrawn"], "WITHDRAWN", lst=lst)
    record(c["expired"], "GRANTED", days_ago=400)
    record(c["tie"], "GRANTED", at=NOW)
    record(c["tie"], "WITHDRAWN", at=NOW)
    record(c["other_list"], "GRANTED", lst=ContactList.objects.create(name="M", stream="MARKETING"))
    return lst, c


EXPECTED = {
    "granted": GRANTED,
    "list_only": GRANTED,
    "withdrawn": CONSENT_WITHDRAWN,
    "regranted": GRANTED,
    "list_withdrawn": CONSENT_WITHDRAWN,  # any current withdrawal wins
    "expired": CONSENT_EXPIRED,
    "tie": CONSENT_WITHDRAWN,  # fail closed
    "nothing": NO_CONSENT,
    "other_list": NO_CONSENT,  # another list's consent does not count here
}


def test_bulk_and_single_agree_on_every_history(histories):
    lst, contacts = histories
    bulk = marketing_consent_bulk([c.pk for c in contacts.values()], lst, now=NOW)
    for name, contact in contacts.items():
        single = marketing_consent(contact.pk, lst, now=NOW)
        assert bulk[contact.pk] == single, name
        assert single.code == EXPECTED[name], name


def test_granted_verdict_names_the_record_relied_on(histories):
    lst, contacts = histories
    verdict = marketing_consent(contacts["regranted"].pk, lst, now=NOW)
    latest_grant = ConsentRecord.objects.get(contact=contacts["regranted"], action="GRANTED")
    assert verdict.record_id == latest_grant.pk


def test_without_a_list_only_stream_wide_records_count(histories):
    _, contacts = histories
    assert marketing_consent(contacts["list_only"].pk, None, now=NOW).code == NO_CONSENT
    assert marketing_consent(contacts["list_withdrawn"].pk, None, now=NOW).code == GRANTED
