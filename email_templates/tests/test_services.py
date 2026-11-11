from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from .. import services
from ..models import ImmutableVersion, TemplateVersion
from ..rendering import NotApproved, render
from . import helpers
from .helpers import AUTHOR_PERMS, GOOD_HTML, GOOD_SAMPLE, GOOD_TEXT, GOOD_VARIABLES, make_user

Status = TemplateVersion.Status


@override_settings(EMAIL_TEMPLATES={"AUDIT_FUNC": "email_templates.tests.helpers.record_audit"})
class LifecycleTests(TestCase):
    def setUp(self):
        helpers.AUDIT_EVENTS.clear()
        self.author = make_user("author", *AUTHOR_PERMS)
        self.approver = make_user("approver", "approve_templateversion")
        self.template, self.v1 = services.create_template(
            self.author,
            name="Welcome",
            category="marketing",
            subject="Hello {{ first_name }}",
            html_body=GOOD_HTML,
            text_body=GOOD_TEXT,
            variables=GOOD_VARIABLES,
            sample_data=GOOD_SAMPLE,
        )

    def actions(self):
        return [e["action"] for e in helpers.AUDIT_EVENTS]

    def test_create_makes_draft_v1_with_checksum_and_audit(self):
        self.assertEqual((self.v1.number, self.v1.status), (1, Status.DRAFT))
        self.assertEqual(self.v1.checksum, self.v1.compute_checksum())
        self.assertEqual(self.template.owner, self.author)
        self.assertEqual(self.actions(), ["email_template.create", "email_template.version.create"])
        self.assertIs(helpers.AUDIT_EVENTS[0]["actor"], self.author)

    def test_edit_draft_changes_checksum_and_audits(self):
        old = self.v1.checksum
        self.v1.subject = "Hi {{ first_name }}"
        services.save_draft(self.author, self.v1)
        self.assertNotEqual(self.v1.checksum, old)
        self.assertEqual(self.actions()[-1], "email_template.version.edit")

    def test_invalid_variables_rejected(self):
        self.v1.variables = {"first name": {}}
        with self.assertRaises(ValidationError):
            services.save_draft(self.author, self.v1)

    def test_full_approval_flow_then_render(self):
        with self.assertRaises(NotApproved):
            render(self.v1, GOOD_SAMPLE, {"unsubscribe_url": "https://example.org/u/1"})
        services.submit(self.author, self.v1)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, Status.PENDING_APPROVAL)
        self.assertEqual(self.v1.preflight_report, [])
        services.approve(self.approver, self.v1)
        self.v1.refresh_from_db()
        self.assertEqual((self.v1.status, self.v1.reviewed_by), (Status.APPROVED, self.approver))
        self.assertEqual(self.template.latest_approved_version(), self.v1)
        out = render(
            self.v1, {"first_name": "Grace"}, {"unsubscribe_url": "https://example.org/u/1"}
        )
        self.assertEqual(out.subject, "Hello Grace")
        self.assertEqual(
            (out.template_version_id, out.template_checksum), (self.v1.pk, self.v1.checksum)
        )
        self.assertEqual(
            self.actions()[-2:], ["email_template.version.submit", "email_template.version.approve"]
        )

    def test_submitted_content_is_frozen(self):
        services.submit(self.author, self.v1)
        self.v1.refresh_from_db()
        self.v1.subject = "Changed"
        with self.assertRaises(ImmutableVersion):
            self.v1.save()
        with self.assertRaises(services.InvalidTransition):
            services.save_draft(self.author, self.v1)

    def test_failed_preflight_keeps_draft_and_saves_report(self):
        self.v1.html_body = "<p>{{ first_name }}</p>"
        services.save_draft(self.author, self.v1)
        with self.assertRaises(services.PreflightFailed):
            services.submit(self.author, self.v1)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, Status.DRAFT)
        self.assertEqual([f["code"] for f in self.v1.preflight_report], ["NO_UNSUBSCRIBE_HTML"])

    def test_permissions(self):
        nobody = make_user("nobody")
        with self.assertRaises(PermissionDenied):
            services.submit(nobody, self.v1)
        services.submit(self.author, self.v1)
        with self.assertRaises(PermissionDenied):
            services.approve(self.author, self.v1)

    def test_reject_and_request_changes_need_a_note(self):
        services.submit(self.author, self.v1)
        with self.assertRaises(ValidationError):
            services.reject(self.approver, self.v1, "  ")
        services.request_changes(self.approver, self.v1, "Add a postal address")
        self.v1.refresh_from_db()
        self.assertEqual(
            (self.v1.status, self.v1.review_note),
            (Status.CHANGES_REQUESTED, "Add a postal address"),
        )
        with self.assertRaises(services.InvalidTransition):
            services.approve(self.approver, self.v1)

    def test_new_version_copies_content_and_retire(self):
        services.submit(self.author, self.v1)
        services.approve(self.approver, self.v1)
        v2 = services.new_version_from(self.author, self.v1)
        self.assertEqual((v2.number, v2.status, v2.checksum), (2, Status.DRAFT, self.v1.checksum))
        services.retire(self.approver, self.v1)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, Status.RETIRED)
        self.assertIsNone(self.template.latest_approved_version())


class AdminTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.admin = get_user_model().objects.create_superuser("admin", password="pw")
        self.client.force_login(self.admin)
        # The host project enforces MFA on superusers (accounts.middleware); a
        # forced test login has none. Harmless under standalone_settings.
        session = self.client.session
        session["mfa_verified_user"] = str(self.admin.pk)
        session.save()
        _, self.version = services.create_template(
            self.admin,
            name="Receipt",
            category="transactional",
            subject="Receipt for {{ first_name }}",
            html_body="<p>Thanks {{ first_name }} <script>x</script></p>",
            text_body="Thanks {{ first_name }}",
            variables=GOOD_VARIABLES,
            sample_data=GOOD_SAMPLE,
        )

    def test_changelist_and_change_pages(self):
        for url in (
            reverse("admin:email_templates_emailtemplate_changelist"),
            reverse("admin:email_templates_templateversion_changelist"),
            reverse("admin:email_templates_emailtemplate_change", args=[self.version.template_id]),
            reverse("admin:email_templates_templateversion_change", args=[self.version.pk]),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_review_page_shows_preflight_and_sandboxed_preview(self):
        url = reverse("admin:email_templates_templateversion_review", args=[self.version.pk])
        body = self.client.get(url).content.decode()
        self.assertIn("UNSAFE_ELEMENT", body)
        self.assertIn('sandbox=""', body)
        self.assertNotIn("<script>x</script>", body)  # escaped inside srcdoc

    def test_submit_action_and_approve_via_review_page(self):
        self.version.html_body = "<p>Thanks {{ first_name }}</p>"
        services.save_draft(self.admin, self.version)
        self.client.post(
            reverse("admin:email_templates_templateversion_changelist"),
            {"action": "submit_for_approval", "_selected_action": [self.version.pk]},
        )
        self.version.refresh_from_db()
        self.assertEqual(self.version.status, Status.PENDING_APPROVAL)
        url = reverse("admin:email_templates_templateversion_review", args=[self.version.pk])
        self.client.post(url, {"decision": "approve", "note": ""})
        self.version.refresh_from_db()
        self.assertEqual(self.version.status, Status.APPROVED)
