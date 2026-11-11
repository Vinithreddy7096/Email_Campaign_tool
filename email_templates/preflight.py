"""Template preflight checks from spec §5.1 step 4, §10.2 and §12 (BUILD.md §10.5.3)."""

import re
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from . import conf
from .rendering import RenderError, render_content

FAIL = "FAIL"
WARN = "WARN"

PREFLIGHT_UNSUBSCRIBE_URL = "https://preflight.invalid/unsubscribe"
UNSAFE_ELEMENTS = {"script", "iframe", "object", "embed", "form", "applet", "base", "meta"}
UNSAFE_SCHEMES = {"javascript", "data", "vbscript"}
_HIDDEN_RE = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden)", re.I)


@dataclass(frozen=True)
class Finding:
    code: str
    level: str
    message: str

    def as_dict(self):
        return asdict(self)


def has_failures(findings):
    return any(f.level == FAIL for f in findings)


def _marker(name):
    if name == "unsubscribe_url":
        return PREFLIGHT_UNSUBSCRIBE_URL
    return f"PREFLIGHT-{name.upper()}"


class _HtmlScan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []  # (href or None, attrs dict)
        self.unsafe = set()
        self.handlers = set()
        self.hidden = 0
        self.imgs_without_alt = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            if (attrs.get("http-equiv") or "").lower() == "refresh":
                self.unsafe.add('<meta http-equiv="refresh">')
        elif tag in UNSAFE_ELEMENTS:
            self.unsafe.add(f"<{tag}>")
        for name in attrs:
            if name.startswith("on"):
                self.handlers.add(name)
        if _HIDDEN_RE.search(attrs.get("style") or ""):
            self.hidden += 1
        if tag == "a":
            self.links.append((attrs.get("href"), attrs))
        if tag == "img" and "alt" not in attrs:
            self.imgs_without_alt += 1

    handle_startendtag = handle_starttag


def preview_system_data():
    """Placeholder values for system variables, used by preflight and previews."""
    names = set(conf.get("SYSTEM_VARIABLES")) | set(conf.get("REQUIRED_MARKETING_VARIABLES"))
    return {name: _marker(name) for name in names}


def run_content(*, category, subject, html_body, text_body, variables, sample_data):
    """Render with sample data and return a list of Findings."""
    findings = []
    variables = variables or {}
    sample_data = sample_data or {}
    marketing = category == "marketing"
    required_marketing = list(conf.get("REQUIRED_MARKETING_VARIABLES"))
    system_data = preview_system_data()
    system_names = set(system_data)

    for name in sorted(set(sample_data) - set(variables)):
        if name not in system_names:
            findings.append(
                Finding(
                    "UNDECLARED_VARIABLE",
                    WARN,
                    f"Sample data has '{name}', which is not declared in variables.",
                )
            )
    try:
        rendered = render_content(
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            variables=variables,
            merge_data=sample_data,
            system_data=system_data,
        )
    except RenderError as exc:
        findings.append(Finding(exc.code, FAIL, str(exc)))
        return findings

    if not rendered.subject:
        findings.append(Finding("EMPTY_SUBJECT", FAIL, "Subject renders empty."))
    if not rendered.html.strip():
        findings.append(Finding("EMPTY_HTML", FAIL, "HTML part renders empty."))
    if not rendered.text.strip():
        findings.append(Finding("EMPTY_TEXT", FAIL, "Plain-text alternative renders empty."))

    html_bytes = len(rendered.html.encode("utf-8"))
    text_bytes = len(rendered.text.encode("utf-8"))
    if html_bytes > conf.get("MAX_HTML_BYTES"):
        findings.append(
            Finding(
                "HTML_TOO_LARGE",
                FAIL,
                f"HTML is {html_bytes} bytes; limit is {conf.get('MAX_HTML_BYTES')}.",
            )
        )
    if text_bytes > conf.get("MAX_TEXT_BYTES"):
        findings.append(
            Finding(
                "TEXT_TOO_LARGE",
                FAIL,
                f"Text is {text_bytes} bytes; limit is {conf.get('MAX_TEXT_BYTES')}.",
            )
        )

    scan = _HtmlScan()
    scan.feed(rendered.html)
    scan.close()

    for element in sorted(scan.unsafe):
        findings.append(Finding("UNSAFE_ELEMENT", FAIL, f"HTML contains {element}."))
    for handler in sorted(scan.handlers):
        findings.append(
            Finding(
                "UNSAFE_ELEMENT", FAIL, f"HTML contains an event handler attribute '{handler}'."
            )
        )
    if scan.hidden:
        findings.append(
            Finding(
                "HIDDEN_CONTENT",
                WARN,
                f"{scan.hidden} element(s) hidden with display:none or visibility:hidden.",
            )
        )
    if scan.imgs_without_alt:
        findings.append(
            Finding(
                "IMG_MISSING_ALT", WARN, f"{scan.imgs_without_alt} image(s) have no alt attribute."
            )
        )

    shorteners = {h.lower() for h in conf.get("URL_SHORTENER_HOSTS")}
    hrefs = []
    for href, attrs in scan.links:
        if href is None:
            if "name" not in attrs and "id" not in attrs:
                findings.append(Finding("EMPTY_LINK", FAIL, "A link has no href."))
            continue
        value = href.strip()
        hrefs.append(value)
        if value in ("", "#"):
            findings.append(Finding("EMPTY_LINK", FAIL, f"A link has an empty href ({href!r})."))
            continue
        parts = urlsplit(value)
        if parts.scheme.lower() in UNSAFE_SCHEMES:
            findings.append(
                Finding("UNSAFE_LINK", FAIL, f"A link uses the {parts.scheme}: scheme.")
            )
            continue
        host = (parts.hostname or "").lower().removeprefix("www.")
        if host in shorteners:
            findings.append(
                Finding("URL_SHORTENER", WARN, f"A link uses the URL shortener {host}.")
            )

    if marketing:
        if PREFLIGHT_UNSUBSCRIBE_URL not in hrefs:
            findings.append(
                Finding(
                    "NO_UNSUBSCRIBE_HTML",
                    FAIL,
                    'Marketing HTML needs a link with href="{{ unsubscribe_url }}".',
                )
            )
        if PREFLIGHT_UNSUBSCRIBE_URL not in rendered.text:
            findings.append(
                Finding(
                    "NO_UNSUBSCRIBE_TEXT", FAIL, "Marketing text part needs {{ unsubscribe_url }}."
                )
            )
        for name in required_marketing:
            if name == "unsubscribe_url":
                continue
            marker = _marker(name)
            for part, body in (("HTML", rendered.html), ("text", rendered.text)):
                if marker not in body:
                    findings.append(
                        Finding(
                            "MISSING_FOOTER_VARIABLE",
                            FAIL,
                            f"Marketing {part} part must include {{{{ {name} }}}}.",
                        )
                    )
    return findings


def run(version):
    return run_content(
        category=version.template.category,
        subject=version.subject,
        html_body=version.html_body,
        text_body=version.text_body,
        variables=version.variables,
        sample_data=version.sample_data,
    )
