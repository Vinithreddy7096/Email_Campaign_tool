"""Contact import from CSV (§6.3): attestation, row checks, consent evidence,
fail-closed skips, all-or-nothing writes, and the console upload flow."""

from datetime import timedelta
from unittest import mock

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role, UserRole
from audit.models import AuditEvent
from common.models import Stream
from consent.models import ConsentRecord
from contacts import imports
from contacts.models import Contact, ContactImport, ContactList, ListMembership
from suppression.models import Suppression
from workers.tasks import contacts as contact_tasks

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def person(django_user_model):
    def make(email, *codes):
        user = django_user_model.objects.create_user(email, "pw", is_staff=True)
        for code in codes:
            UserRole.grant(user, code, by=None)
        return django_user_model.objects.get(pk=user.pk)

    return make


@pytest.fixture
def manager(person):
    return person("manager@example.com", Role.Code.CAMPAIGN_MANAGER)


@pytest.fixture
def newsletter():
    return ContactList.objects.create(name="Newsletter", stream=Stream.MARKETING)


@pytest.fixture(autouse=True)
def no_broker():
    """start() queues the task on commit; tests run process() themselves."""
    with mock.patch.object(contact_tasks.process_import, "delay") as delay:
        yield delay


def upload(actor, lst, text, **kw):
    fields = {"filename": "people.csv", "consent_source": "Sign-up form on example.com",
              "attestation_text": "These people subscribed on our site."}  # fmt: skip
    fields.update(kw)
    imp = imports.start(actor, contact_list=lst, content=text.encode("utf-8"), **fields)
    return imports.process(imp.pk)


def codes(imp):
    return sorted(i["code"] for i in imp.issues)


# ─── Happy path ──────────────────────────────────────────────────────────


def test_import_adds_contacts_memberships_and_list_scoped_consent(manager, newsletter):
    imp = upload(manager, newsletter,
                 "Email,First Name,last_name,Plan\n"
                 "Ada@Example.com,Ada,Lovelace,pro\n"
                 "grace@example.com,Grace,,\n")  # fmt: skip
    assert imp.status == "COMPLETED" and imp.total_rows == 2 and imp.imported_count == 2
    ada = Contact.objects.get(email="ada@example.com")  # normalized
    assert (ada.first_name, ada.last_name, ada.attributes) == ("Ada", "Lovelace", {"plan": "pro"})
    assert Contact.objects.get(email="grace@example.com").attributes == {}  # empty cells dropped
    assert ListMembership.objects.filter(contact_list=newsletter, source_import=imp).count() == 2

    record = ConsentRecord.objects.get(contact=ada)
    assert (record.stream, record.contact_list, record.action, record.method) == (
        Stream.MARKETING, newsletter, "GRANTED", "IMPORT")  # fmt: skip
    assert record.source_import == imp and record.recorded_by == manager
    assert record.occurred_at == imp.attested_at  # no consent_date column
    assert record.evidence["row"] == 2 and record.evidence["file_sha256"] == imp.file.sha256
    assert AuditEvent.objects.filter(action="contact_import.create", actor=manager).exists()
    assert AuditEvent.objects.filter(action="contact_import.complete").exists()


def test_consent_date_column_is_used_and_validated(manager, newsletter):
    future = (timezone.now() + timedelta(days=3)).date().isoformat()
    imp = upload(manager, newsletter,
                 "email,consent_date\n"
                 "a@example.com,2024-05-01\n"
                 "b@example.com,2024-05-01T10:30:00+00:00\n"
                 f"c@example.com,{future}\n"
                 "d@example.com,last spring\n")  # fmt: skip
    assert imp.imported_count == 2 and imp.skipped_invalid == 2
    assert codes(imp) == ["BAD_CONSENT_DATE", "BAD_CONSENT_DATE"]
    a = ConsentRecord.objects.get(contact__email="a@example.com")
    assert a.occurred_at.date().isoformat() == "2024-05-01"
    assert a.evidence["consent_date_from_file"] is True


def test_semicolon_files_and_a_byte_order_mark_are_read(manager, newsletter):
    imp = imports.start(manager, contact_list=newsletter, filename="excel.csv",
                        content="﻿email;first_name\nx@example.com;X\ny@example.com;Y\n".encode(),
                        consent_source="Form", attestation_text="Yes")  # fmt: skip
    imp = imports.process(imp.pk)
    assert imp.imported_count == 2
    assert Contact.objects.get(email="x@example.com").first_name == "X"


def test_transactional_lists_get_members_but_no_consent_records(manager):
    lst = ContactList.objects.create(name="Customers", stream=Stream.TRANSACTIONAL)
    imp = upload(manager, lst, "email\nbuyer@example.com\n")
    assert imp.imported_count == 1
    assert ListMembership.objects.filter(contact_list=lst).count() == 1
    assert not ConsentRecord.objects.exists()


# ─── Rows that are skipped ───────────────────────────────────────────────


def test_invalid_and_duplicate_rows_are_skipped_with_row_numbers(manager, newsletter):
    imp = upload(manager, newsletter,
                 "email,first_name\n"
                 "ok@example.com,A\n"
                 "not-an-address,B\n"
                 "OK@example.com,C\n"
                 "\n"
                 "x@example.com,D,extra\n"
                 f"long@example.com,{'n' * 151}\n")  # fmt: skip
    assert imp.total_rows == 5  # the blank line is not a row
    assert (imp.imported_count, imp.skipped_invalid, imp.skipped_duplicate) == (1, 3, 1)
    assert {(i["row"], i["code"]) for i in imp.issues} == {
        (3, "INVALID_EMAIL"), (4, "DUPLICATE_IN_FILE"), (6, "WRONG_COLUMN_COUNT"),
        (7, "NAME_TOO_LONG"),
    }  # fmt: skip
    assert not any("@" in i["detail"] for i in imp.issues)  # never store addresses in issues


def test_existing_members_are_duplicates_and_existing_contacts_are_not_overwritten(
    manager, newsletter
):
    member = Contact.objects.create(email="member@example.com", first_name="Kept")
    ListMembership.objects.create(contact=member, contact_list=newsletter)
    other = Contact.objects.create(email="other@example.com", first_name="Original")
    imp = upload(manager, newsletter,
                 "email,first_name\nmember@example.com,New\nother@example.com,Changed\n")  # fmt: skip
    assert (imp.imported_count, imp.skipped_duplicate) == (1, 1)
    assert codes(imp) == ["ALREADY_ON_LIST"]
    other.refresh_from_db()
    assert other.first_name == "Original"  # a file never overwrites a contact
    assert ListMembership.objects.filter(contact=other, contact_list=newsletter).exists()
    assert not ConsentRecord.objects.filter(contact=member).exists()


def test_suppressed_addresses_and_domains_are_skipped(manager, newsletter):
    Suppression.suppress("bounced@example.com", Suppression.Reason.HARD_BOUNCE)
    Suppression.suppress_domain("blocked.example", Suppression.Reason.MANUAL)
    imp = upload(manager, newsletter,
                 "email\nbounced@example.com\nanyone@blocked.example\nfine@example.com\n")  # fmt: skip
    assert (imp.imported_count, imp.skipped_suppressed) == (1, 2)
    assert not Contact.objects.filter(email="bounced@example.com").exists()


def test_a_file_never_resubscribes_someone_who_withdrew_or_was_erased(manager, newsletter):
    left = Contact.objects.create(email="left@example.com")
    ConsentRecord.objects.create(contact=left, stream=Stream.MARKETING, action="GRANTED",
                                 method="WEB_FORM")  # fmt: skip
    ConsentRecord.objects.create(contact=left, stream=Stream.MARKETING, action="WITHDRAWN",
                                 method="MANUAL")  # fmt: skip
    gone = Contact.objects.create(email="gone@example.com")
    gone.erase()
    imp = upload(manager, newsletter, "email\nleft@example.com\ngone@example.com\n")
    assert (imp.imported_count, imp.skipped_opted_out) == (0, 2)
    assert codes(imp) == ["CONSENT_WITHDRAWN", "ERASED"]
    assert not ListMembership.objects.filter(contact_list=newsletter).exists()
    assert not Contact.objects.live().filter(email="gone@example.com").exists()


# ─── Whole-file rejections ───────────────────────────────────────────────


@pytest.mark.parametrize(("content", "message"), [
    (b"name\nAda\n", 'No "email" column'),
    (b"email\n", "no rows"),
    (b"email,Email\nx@example.com,y@example.com\n", "appears twice"),
    (b"email,plan!\nx@example.com,pro\n", "unusable name"),
    ("email\nzoë@example.com\n".encode("latin-1"), "not UTF-8"),
])  # fmt: skip
def test_bad_files_are_rejected_whole(manager, newsletter, content, message):
    imp = imports.start(manager, contact_list=newsletter, filename="f.csv", content=content,
                        consent_source="Form", attestation_text="Yes")  # fmt: skip
    imp = imports.process(imp.pk)
    assert imp.status == "REJECTED" and message in imp.error
    assert not Contact.objects.exists()


def test_too_many_rows_rejects_the_whole_file(manager, newsletter, settings):
    settings.CONTACT_IMPORT_MAX_ROWS = 2
    imp = upload(manager, newsletter, "email\na@example.com\nb@example.com\nc@example.com\n")
    assert imp.status == "REJECTED" and "CONTACT_IMPORT_MAX_ROWS" in imp.error
    assert not Contact.objects.exists()


def test_a_crash_mid_import_leaves_nothing_behind(manager, newsletter):
    imp = imports.start(manager, contact_list=newsletter, filename="f.csv",
                        content=b"email\na@example.com\nb@example.com\n",
                        consent_source="Form", attestation_text="Yes")  # fmt: skip
    with mock.patch.object(ConsentRecord.objects, "bulk_create", side_effect=RuntimeError("boom")):
        failed = imports.process(imp.pk)
    assert failed.status == "FAILED" and "boom" in failed.error
    assert not Contact.objects.exists() and not ListMembership.objects.exists()


def test_processing_is_idempotent(manager, newsletter):
    imp = upload(manager, newsletter, "email\na@example.com\n")
    again = imports.process(imp.pk)
    assert again.status == "COMPLETED" and again.imported_count == 1
    assert ConsentRecord.objects.count() == 1


# ─── Who may import, and what ────────────────────────────────────────────


def test_start_checks_permission_attestation_size_and_list(person, manager, newsletter, settings):
    analyst = person("analyst@example.com", Role.Code.ANALYST)
    ok = {"filename": "f.csv", "content": b"email\na@example.com\n",
          "consent_source": "Form", "attestation_text": "Yes"}  # fmt: skip
    with pytest.raises(PermissionDenied):
        imports.start(analyst, contact_list=newsletter, **ok)
    with pytest.raises(ValidationError, match="attest"):
        imports.start(manager, contact_list=newsletter, **{**ok, "attestation_text": "  "})
    settings.CONTACT_IMPORT_MAX_BYTES = 5
    with pytest.raises(ValidationError, match="CONTACT_IMPORT_MAX_BYTES"):
        imports.start(manager, contact_list=newsletter, **ok)
    settings.CONTACT_IMPORT_MAX_BYTES = 1000
    newsletter.archived_at = timezone.now()
    newsletter.save()
    with pytest.raises(ValidationError, match="archived"):
        imports.start(manager, contact_list=newsletter, **ok)
    assert not ContactImport.objects.exists()


def test_the_task_is_queued_only_after_commit(manager, newsletter, no_broker,
                                              django_capture_on_commit_callbacks):  # fmt: skip
    with django_capture_on_commit_callbacks(execute=True):
        imp = imports.start(manager, contact_list=newsletter, filename="f.csv",
                            content=b"email\na@example.com\n", consent_source="Form",
                            attestation_text="Yes")  # fmt: skip
        no_broker.assert_not_called()
    no_broker.assert_called_once_with(str(imp.pk))


def test_resume_requeues_lost_and_stuck_imports_only(manager, newsletter, no_broker):
    def make(status, age_minutes):
        imp = imports.start(manager, contact_list=newsletter, filename="f.csv",
                            content=b"email\na@example.com\n", consent_source="F",
                            attestation_text="Y")  # fmt: skip
        ContactImport.objects.filter(pk=imp.pk).update(
            status=status, updated_at=timezone.now() - timedelta(minutes=age_minutes))
        return str(imp.pk)

    lost = make("PENDING", 5)
    make("PENDING", 0)  # just uploaded; its task may still be on the way
    stuck = make("IMPORTING", 60)
    make("IMPORTING", 5)  # still running
    make("COMPLETED", 600)
    no_broker.reset_mock()
    assert contact_tasks.resume_imports() == 2
    assert sorted(c.args[0] for c in no_broker.call_args_list) == sorted([lost, stuck])


# ─── Console ─────────────────────────────────────────────────────────────


def test_console_upload_flow(manager, newsletter, login_verified, no_broker):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = login_verified(manager)
    assert client.get(reverse("console:contact_import_new")).status_code == 200
    data = {"contact_list": newsletter.pk, "consent_source": "Sign-up form",
            "attestation_text": "They subscribed.", "confirm": "on",
            "file": SimpleUploadedFile("p.csv", b"email\nweb@example.com\n", "text/csv")}  # fmt: skip
    response = client.post(reverse("console:contact_import_new"), data)
    imp = ContactImport.objects.get()
    assert response.status_code == 302 and response.url == reverse("console:contact_import", args=[imp.pk])
    page = client.get(response.url).content.decode()
    assert "refreshes by itself" in page and "location.reload" in page
    imports.process(imp.pk)
    page = client.get(response.url).content.decode()
    assert "Completed" in page and "location.reload" not in page
    assert "p.csv" in client.get(reverse("console:audience")).content.decode()


def test_console_upload_requires_the_confirmation_box(manager, newsletter, login_verified):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client = login_verified(manager)
    response = client.post(reverse("console:contact_import_new"), {
        "contact_list": newsletter.pk, "consent_source": "Form", "attestation_text": "Yes",
        "file": SimpleUploadedFile("p.csv", b"email\na@example.com\n")})  # fmt: skip
    assert response.status_code == 200 and not ContactImport.objects.exists()


def test_analysts_cannot_open_the_import_page(person, login_verified):
    client = login_verified(person("analyst@example.com", Role.Code.ANALYST))
    assert client.get(reverse("console:contact_import_new")).status_code == 403
