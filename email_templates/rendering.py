"""Deterministic, escaped rendering of a TemplateVersion (BUILD.md §10.5.2)."""

import hashlib
import re
from dataclasses import dataclass
from functools import cache

from django.template import Context, Engine, TemplateSyntaxError

from . import conf

MAX_SUBJECT_CHARS = 998  # RFC 5322 line length limit
_MISSING = "\x00MISSING:%s\x00"
_MISSING_RE = re.compile("\x00MISSING:([^\x00]*)\x00")


class RenderError(Exception):
    code = "RENDER_ERROR"


class TemplateInvalid(RenderError):
    def __init__(self, part, message):
        if "Invalid block tag" in message:
            self.code = "DISALLOWED_TAG"
        elif "Invalid filter" in message:
            self.code = "DISALLOWED_FILTER"
        else:
            self.code = "SYNTAX_ERROR"
        self.part = part
        super().__init__(f"{part}: {message}")


class MissingVariables(RenderError):
    code = "MISSING_VARIABLE"

    def __init__(self, names):
        self.names = sorted(set(names))
        super().__init__("Missing variables: " + ", ".join(self.names))


class InvalidMergeData(RenderError):
    code = "INVALID_MERGE_DATA"


class HeaderInjection(RenderError):
    code = "SUBJECT_HEADER_INJECTION"


class NotApproved(RenderError):
    code = "NOT_APPROVED"


@dataclass(frozen=True)
class RenderedMessage:
    subject: str
    html: str
    text: str
    content_hash: str
    template_version_id: int | None
    template_checksum: str


class _LockedEngine(Engine):
    # Drop Django's default builtins ({% load %}, {% include %}, |safe, ...).
    default_builtins = []


@cache
def _engine(autoescape):
    return _LockedEngine(
        builtins=[f"{__package__}._library"],
        autoescape=autoescape,
        string_if_invalid=_MISSING,
    )


def compile_part(part, source, *, autoescape):
    try:
        return _engine(autoescape).from_string(source)
    except TemplateSyntaxError as exc:
        raise TemplateInvalid(part, str(exc)) from exc


def _check_json(value, path="merge data"):
    if value is None or isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check_json(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidMergeData(f"{path}: keys must be strings")
            _check_json(item, f"{path}.{key}")
        return
    raise InvalidMergeData(f"{path}: {type(value).__name__} is not JSON data")


def build_context(variables, merge_data, system_data):
    """Declared defaults, then merge data, then system data (system wins)."""
    merge_data = merge_data or {}
    system_data = system_data or {}
    for name, data in (("merge data", merge_data), ("system data", system_data)):
        if not isinstance(data, dict):
            raise InvalidMergeData(f"{name} must be an object")
        _check_json(data, name)

    system_names = set(conf.get("SYSTEM_VARIABLES"))
    context, missing = {}, []
    for name, spec in (variables or {}).items():
        if name in system_names:
            continue
        if name in merge_data:
            continue
        if "default" in spec:
            context[name] = spec["default"]
        elif spec.get("required", True):
            missing.append(name)
        else:
            context[name] = ""
    if missing:
        raise MissingVariables(missing)
    context.update({k: v for k, v in merge_data.items() if k not in system_names})
    context.update(system_data)
    return context


def _render_part(part, source, context, *, autoescape):
    output = compile_part(part, source, autoescape=autoescape).render(
        Context(context, autoescape=autoescape)
    )
    # Django returns SafeString; downgrade to str so any page that later shows
    # this output (admin preview, campaign UI) escapes it (spec §12 stored XSS).
    output = str.__str__(output)
    missing = _MISSING_RE.findall(output)
    if missing:
        raise MissingVariables(missing)
    return output


def render_content(
    *,
    subject,
    html_body,
    text_body,
    variables,
    merge_data,
    system_data,
    template_version_id=None,
    template_checksum="",
):
    context = build_context(variables, merge_data, system_data)
    subject_out = _render_part("subject", subject, context, autoescape=False).strip()
    if "\r" in subject_out or "\n" in subject_out:
        raise HeaderInjection("Rendered subject contains a line break")
    if len(subject_out) > MAX_SUBJECT_CHARS:
        raise HeaderInjection(f"Rendered subject is longer than {MAX_SUBJECT_CHARS} characters")
    html_out = _render_part("html", html_body, context, autoescape=True)
    text_out = _render_part("text", text_body, context, autoescape=False)
    digest = hashlib.sha256()
    for piece in (subject_out, html_out, text_out):
        digest.update(piece.encode("utf-8"))
        digest.update(b"\x00")
    return RenderedMessage(
        subject_out, html_out, text_out, digest.hexdigest(), template_version_id, template_checksum
    )


def render(version, merge_data, system_data=None, *, require_approved=True):
    """Render a TemplateVersion for one recipient.

    The delivery pipeline stores content_hash and template_checksum with the
    send (spec §10.2). Only APPROVED versions render unless require_approved
    is False (previews and preflight).
    """
    if require_approved and version.status != version.Status.APPROVED:
        raise NotApproved(f"Template version {version.pk} is {version.status}, not APPROVED")
    return render_content(
        subject=version.subject,
        html_body=version.html_body,
        text_body=version.text_body,
        variables=version.variables,
        merge_data=merge_data,
        system_data=system_data,
        template_version_id=version.pk,
        template_checksum=version.checksum,
    )
