from django.test import SimpleTestCase

from ..rendering import (
    HeaderInjection,
    InvalidMergeData,
    MissingVariables,
    TemplateInvalid,
    render_content,
)


def render(
    subject="Hi {{ name }}",
    html="<p>{{ name }}</p>",
    text="{{ name }}",
    variables=None,
    merge=None,
    system=None,
):
    return render_content(
        subject=subject,
        html_body=html,
        text_body=text,
        variables=variables if variables is not None else {"name": {"required": True}},
        merge_data=merge if merge is not None else {"name": "Ada"},
        system_data=system or {},
    )


class RenderTests(SimpleTestCase):
    def test_renders_all_parts(self):
        out = render()
        self.assertEqual((out.subject, out.html, out.text), ("Hi Ada", "<p>Ada</p>", "Ada"))
        self.assertEqual(len(out.content_hash), 64)

    def test_html_is_escaped_but_text_and_subject_are_not(self):
        out = render(merge={"name": "<script>x</script> & co"})
        self.assertEqual(out.html, "<p>&lt;script&gt;x&lt;/script&gt; &amp; co</p>")
        self.assertEqual(out.text, "<script>x</script> & co")
        self.assertEqual(out.subject, "Hi <script>x</script> & co")

    def test_missing_required_variable(self):
        with self.assertRaises(MissingVariables) as ctx:
            render(merge={})
        self.assertEqual(ctx.exception.names, ["name"])

    def test_undeclared_missing_variable_is_an_error(self):
        with self.assertRaises(MissingVariables) as ctx:
            render(html="<p>{{ name }} {{ contact.city }}</p>")
        self.assertEqual(ctx.exception.names, ["contact.city"])

    def test_missing_variable_with_default_filter_is_still_an_error(self):
        with self.assertRaises(MissingVariables):
            render(html="<p>{{ other|default:'x' }}</p>")

    def test_optional_and_default_variables(self):
        variables = {
            "name": {"required": True},
            "nick": {"required": False},
            "team": {"required": False, "default": "Support"},
        }
        out = render(
            html="<p>{% if nick %}{{ nick }}{% else %}{{ name }}{% endif %} / {{ team }}</p>",
            variables=variables,
        )
        self.assertEqual(out.html, "<p>Ada / Support</p>")
        out = render(
            html="<p>{{ nick }}{{ team }}</p>",
            variables=variables,
            merge={"name": "Ada", "nick": "A", "team": "Ops"},
        )
        self.assertEqual(out.html, "<p>AOps</p>")

    def test_loops_and_allowed_filters(self):
        out = render(
            html="{% for i in items %}{{ i.n|upper }}{% if not forloop.last %},{% endif %}{% endfor %}",
            variables={},
            merge={"name": "Ada", "items": [{"n": "a"}, {"n": "b"}]},
        )
        self.assertEqual(out.html, "A,B")

    def test_disallowed_tags_and_filters(self):
        cases = {
            "{% load static %}": "DISALLOWED_TAG",
            "{% include 'x.html' %}": "DISALLOWED_TAG",
            "{% now 'Y' %}": "DISALLOWED_TAG",
            "{% autoescape off %}{{ name }}{% endautoescape %}": "DISALLOWED_TAG",
            "{% url 'x' %}": "DISALLOWED_TAG",
            "{{ name|safe }}": "DISALLOWED_FILTER",
            "{{ name|random }}": "DISALLOWED_FILTER",
            "{% if %}": "SYNTAX_ERROR",
        }
        for source, code in cases.items():
            with self.subTest(source=source), self.assertRaises(TemplateInvalid) as ctx:
                render(html=source)
            self.assertEqual(ctx.exception.code, code)

    def test_subject_header_injection(self):
        with self.assertRaises(HeaderInjection):
            render(merge={"name": "Ada\r\nBcc: victim@example.com"})
        with self.assertRaises(HeaderInjection):
            render(merge={"name": "x" * 1000})

    def test_subject_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(render(subject="  Hi {{ name }}\n").subject, "Hi Ada")

    def test_merge_data_must_be_json(self):
        with self.assertRaises(InvalidMergeData):
            render(merge={"name": object()})
        with self.assertRaises(InvalidMergeData):
            render(merge=["not", "a", "dict"])

    def test_system_data_wins_over_merge_data(self):
        out = render(
            html='<a href="{{ unsubscribe_url }}">u</a>{{ name }}',
            merge={"name": "Ada", "unsubscribe_url": "https://evil.example/"},
            system={"unsubscribe_url": "https://example.org/u/abc"},
        )
        self.assertIn("https://example.org/u/abc", out.html)
        self.assertNotIn("evil", out.html)

    def test_output_is_plain_str_not_marked_safe(self):
        from django.utils.safestring import SafeData

        out = render()
        for part in (out.subject, out.html, out.text):
            self.assertNotIsInstance(part, SafeData)

    def test_content_hash_is_deterministic(self):
        self.assertEqual(render().content_hash, render().content_hash)
        self.assertNotEqual(render().content_hash, render(merge={"name": "Bob"}).content_hash)
