from django.test import SimpleTestCase, override_settings

from ..preflight import FAIL, WARN, has_failures, run_content
from .helpers import GOOD_HTML, GOOD_SAMPLE, GOOD_TEXT, GOOD_VARIABLES


def check(
    category="marketing",
    subject="Hello {{ first_name }}",
    html=GOOD_HTML,
    text=GOOD_TEXT,
    variables=GOOD_VARIABLES,
    sample=GOOD_SAMPLE,
):
    findings = run_content(
        category=category,
        subject=subject,
        html_body=html,
        text_body=text,
        variables=variables,
        sample_data=sample,
    )
    return {(f.code, f.level) for f in findings}, findings


class PreflightTests(SimpleTestCase):
    def test_good_marketing_template_passes(self):
        codes, findings = check()
        self.assertEqual(codes, set(), findings)

    def test_marketing_needs_unsubscribe_in_both_parts(self):
        codes, _ = check(html="<p>Hi {{ first_name }}</p>", text="Hi {{ first_name }}")
        self.assertIn(("NO_UNSUBSCRIBE_HTML", FAIL), codes)
        self.assertIn(("NO_UNSUBSCRIBE_TEXT", FAIL), codes)

    def test_unsubscribe_must_be_a_link_not_just_text(self):
        codes, _ = check(html="<p>Hi {{ first_name }} {{ unsubscribe_url }}</p>")
        self.assertIn(("NO_UNSUBSCRIBE_HTML", FAIL), codes)

    def test_transactional_does_not_need_unsubscribe(self):
        codes, _ = check(
            category="transactional", html="<p>Hi {{ first_name }}</p>", text="Hi {{ first_name }}"
        )
        self.assertEqual(codes, set())

    def test_missing_sample_variable_fails(self):
        codes, _ = check(sample={})
        self.assertEqual(codes, {("MISSING_VARIABLE", FAIL)})

    def test_undeclared_sample_variable_warns(self):
        codes, _ = check(sample={**GOOD_SAMPLE, "extra": 1})
        self.assertEqual(codes, {("UNDECLARED_VARIABLE", WARN)})

    def test_syntax_error_fails(self):
        codes, _ = check(html="{% load static %}")
        self.assertEqual(codes, {("DISALLOWED_TAG", FAIL)})

    def test_empty_parts_fail(self):
        codes, _ = check(category="transactional", subject=" ", html=" ", text=" ")
        self.assertTrue(
            {("EMPTY_SUBJECT", FAIL), ("EMPTY_HTML", FAIL), ("EMPTY_TEXT", FAIL)} <= codes
        )

    def test_empty_and_unsafe_links(self):
        html = (
            GOOD_HTML
            + '<a href="">a</a><a href="#">b</a><a>c</a><a name="top"></a><a href="javascript:alert(1)">d</a>'
        )
        _, findings = check(html=html)
        self.assertEqual(sum(f.code == "EMPTY_LINK" for f in findings), 3)
        self.assertEqual(sum(f.code == "UNSAFE_LINK" for f in findings), 1)

    def test_link_that_renders_empty_fails(self):
        codes, _ = check(
            html=GOOD_HTML + '<a href="{{ link }}">x</a>',
            variables={**GOOD_VARIABLES, "link": {"required": False}},
        )
        self.assertIn(("EMPTY_LINK", FAIL), codes)

    def test_unsafe_elements(self):
        html = (
            GOOD_HTML
            + '<script>x</script><iframe src="https://x"></iframe><div onclick="x()">y</div>'
        )
        _, findings = check(html=html)
        messages = " ".join(f.message for f in findings if f.code == "UNSAFE_ELEMENT")
        for needle in ("<script>", "<iframe>", "onclick"):
            self.assertIn(needle, messages)

    def test_content_type_meta_is_fine_but_refresh_is_not(self):
        ok = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">' + GOOD_HTML
        self.assertEqual(check(html=ok)[0], set())
        bad = '<meta http-equiv="refresh" content="0;url=https://x">' + GOOD_HTML
        self.assertIn(("UNSAFE_ELEMENT", FAIL), check(html=bad)[0])

    def test_warnings_do_not_fail(self):
        html = (
            GOOD_HTML
            + '<img src="https://example.org/a.png"><div style="display: none">x</div><a href="https://bit.ly/x">s</a>'
        )
        codes, findings = check(html=html)
        self.assertEqual(
            codes, {("IMG_MISSING_ALT", WARN), ("HIDDEN_CONTENT", WARN), ("URL_SHORTENER", WARN)}
        )
        self.assertFalse(has_failures(findings))

    @override_settings(EMAIL_TEMPLATES={"MAX_HTML_BYTES": 100, "MAX_TEXT_BYTES": 10})
    def test_oversized(self):
        codes, _ = check()
        self.assertTrue({("HTML_TOO_LARGE", FAIL), ("TEXT_TOO_LARGE", FAIL)} <= codes)

    @override_settings(
        EMAIL_TEMPLATES={"REQUIRED_MARKETING_VARIABLES": ["unsubscribe_url", "postal_address"]}
    )
    def test_required_footer_variables(self):
        codes, _ = check()
        self.assertIn(("MISSING_FOOTER_VARIABLE", FAIL), codes)
        codes, _ = check(
            html=GOOD_HTML.replace("</body>", "{{ postal_address }}</body>"),
            text=GOOD_TEXT + "{{ postal_address }}",
        )
        self.assertEqual(codes, set())
