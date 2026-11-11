"""Unsubscribe (BUILD.md §10.8.4, RFC 8058): public, no login, idempotent."""

import pytest
from django.urls import reverse

from common.models import Stream
from consent.evaluation import marketing_consent
from consent.models import ConsentRecord
from contacts.models import Contact
from suppression import unsubscribe
from suppression.models import Suppression

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def contact():
    c = Contact.objects.create(email="bob@example.com")
    ConsentRecord.objects.create(
        contact=c, stream=Stream.MARKETING, action="GRANTED", method="WEB_FORM"
    )
    return c


def url_for(contact, campaign="camp-1"):
    return reverse("unsubscribe", args=[unsubscribe.make_token(contact.pk, campaign)])


def test_token_is_deterministic_so_rendering_is(contact):
    """The pipeline re-renders at handoff; a timestamped token would change the content."""
    assert unsubscribe.make_token(contact.pk, "c") == unsubscribe.make_token(contact.pk, "c")
    assert unsubscribe.unsubscribe_url(contact.pk).startswith("http")


def test_get_only_confirms_it_does_not_unsubscribe(client, contact):
    """Link scanners follow GETs; they must not unsubscribe anyone."""
    page = client.get(url_for(contact))
    assert page.status_code == 200 and "<form" in page.content.decode()
    assert not Suppression.objects.exists()


def test_post_unsubscribes_and_withdraws_consent(client, contact):
    page = client.post(url_for(contact))
    assert page.status_code == 200 and "unsubscribed" in page.content.decode()
    row = Suppression.objects.get()
    assert (row.scope, row.stream, row.reason) == ("STREAM", "MARKETING", "UNSUBSCRIBE")
    assert row.source == "unsubscribe:campaign=camp-1"
    assert marketing_consent(contact.pk).code == "CONSENT_WITHDRAWN"
    withdrawal = ConsentRecord.objects.get(action="WITHDRAWN")
    assert withdrawal.method == "UNSUBSCRIBE_LINK"


def test_rfc8058_one_click(client, contact):
    resp = client.post(url_for(contact), {"List-Unsubscribe": "One-Click"})
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/plain")
    assert ConsentRecord.objects.get(action="WITHDRAWN").method == "LIST_UNSUBSCRIBE"


def test_idempotent(client, contact):
    for _ in range(3):
        client.post(url_for(contact))
    assert Suppression.objects.count() == 1
    assert ConsentRecord.objects.filter(action="WITHDRAWN").count() == 1


@pytest.mark.parametrize("token", ["garbage", "a:b:c"])
def test_bad_tokens(client, token):
    assert client.get(reverse("unsubscribe", args=[token])).status_code == 400
    assert client.post(reverse("unsubscribe", args=[token])).status_code == 400


def test_tampered_token_is_rejected(client, contact):
    token = unsubscribe.make_token(contact.pk)
    assert client.post(reverse("unsubscribe", args=[token[:-2] + "xx"])).status_code == 400


def test_logged_in_staff_are_not_sent_to_mfa(client, contact, django_user_model):
    root = django_user_model.objects.create_superuser("root@example.com", "pw")
    client.force_login(root)  # superuser, MFA not verified
    assert client.get(url_for(contact)).status_code == 200


def test_telemetry_failure_never_blocks_unsubscribing(client, contact, monkeypatch):
    from workers.services import events

    def boom(*args, **kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(events, "record_unsubscribe_link", boom)
    assert client.post(url_for(contact)).status_code == 200
    assert Suppression.objects.filter(reason="UNSUBSCRIBE").exists()


def test_erased_contact_still_withdraws(client, contact):
    contact.erase()
    assert client.post(url_for(contact)).status_code == 200
    assert marketing_consent(contact.pk).code == "CONSENT_WITHDRAWN"
