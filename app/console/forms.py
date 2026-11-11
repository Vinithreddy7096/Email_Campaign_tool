"""Console forms. Validation of meaning lives in the services; these only shape input."""

from __future__ import annotations

import json
from typing import Any, cast

from django import forms

from common.models import Stream
from contacts.models import ContactList, Segment
from domains.models import SenderIdentity
from email_templates.models import EmailTemplate, TemplateVersion
from suppression.models import Suppression


class CampaignForm(forms.Form):
    name = forms.CharField(max_length=200)
    purpose = forms.ChoiceField(choices=Stream.choices)
    sender_identity = forms.ModelChoiceField(
        queryset=SenderIdentity.objects.filter(is_active=True).select_related("domain")
    )
    template_version = forms.ModelChoiceField(
        queryset=TemplateVersion.objects.filter(
            status=TemplateVersion.Status.APPROVED
        ).select_related("template"),
        help_text="Only approved versions can be used.",
    )
    audience_list = forms.ModelChoiceField(
        queryset=ContactList.objects.filter(archived_at__isnull=True)
    )
    audience_segment = forms.ModelChoiceField(
        queryset=Segment.objects.filter(contact_list__archived_at__isnull=True)
        .select_related("contact_list").order_by("contact_list__name", "name"),
        required=False, label="Segment (optional)",
        help_text="Narrows the list. Must be a segment of the list above.",
    )  # fmt: skip
    scheduled_at = forms.DateTimeField(
        required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="UTC. Optional; you schedule after approval.",
    )  # fmt: skip

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["sender_identity"].label_from_instance = lambda s: str(s)  # type: ignore[attr-defined]


def _json_object(value: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise forms.ValidationError(
            f"{what} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from exc
    if not isinstance(data, dict):
        raise forms.ValidationError(f"{what} must be a JSON object.")
    return data


class VersionContentForm(forms.Form):
    subject = forms.CharField(max_length=998)
    html_body = forms.CharField(widget=forms.Textarea(attrs={"rows": 12}), label="HTML body")
    text_body = forms.CharField(widget=forms.Textarea(attrs={"rows": 7}), label="Plain-text body")
    variables = forms.CharField(widget=forms.Textarea(attrs={"rows": 6}), required=False,
                                help_text='{"first_name": {"required": true}}')  # fmt: skip
    sample_data = forms.CharField(widget=forms.Textarea(attrs={"rows": 6}), required=False,
                                  label="Sample data (JSON)")  # fmt: skip

    def clean_variables(self) -> dict[str, Any]:
        return _json_object(self.cleaned_data["variables"], "Variables")

    def clean_sample_data(self) -> dict[str, Any]:
        return _json_object(self.cleaned_data["sample_data"], "Sample data")

    @staticmethod
    def initial_for(version: TemplateVersion) -> dict[str, Any]:
        return {
            "subject": version.subject,
            "html_body": version.html_body,
            "text_body": version.text_body,
            "variables": json.dumps(version.variables, indent=1),
            "sample_data": json.dumps(version.sample_data, indent=1),
        }


class NewTemplateForm(VersionContentForm):
    name = forms.CharField(max_length=200)
    category = forms.ChoiceField(choices=EmailTemplate.Category.choices)
    description = forms.CharField(required=False)
    field_order = ["name", "category", "description", "subject", "html_body", "text_body",
                   "variables", "sample_data"]  # fmt: skip


# ─── Contacts: import and segments ───────────────────────────────────────

PROHIBITED_SOURCES = ("purchased", "rented", "scraped", "harvested")


class ContactImportForm(forms.Form):
    contact_list = forms.ModelChoiceField(
        queryset=ContactList.objects.filter(archived_at__isnull=True).order_by("name"),
        label="Add to list",
    )
    file = forms.FileField(
        label="CSV file (UTF-8)",
        help_text="Header row required with an email column. Optional: first_name, last_name, "
                  "consent_date. Other columns become attributes.",
    )  # fmt: skip
    consent_source = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), label="Where consent came from",
        help_text="e.g. Newsletter sign-up form on example.com, double opt-in, 2024–2026",
    )  # fmt: skip
    attestation_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), label="Attestation (kept with every record)",
        help_text="State, in your own words, that these people asked to receive this mail.",
    )  # fmt: skip
    confirm = forms.BooleanField(
        label="I confirm this list was not purchased, rented, scraped or harvested, "
              "and that I am accountable for this attestation.",
    )  # fmt: skip


class SegmentForm(forms.Form):
    name = forms.CharField(max_length=200)
    definition = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12}), required=False, label="Rules (JSON)",
        help_text='{} = every member. e.g. {"all": [{"field": "email_domain", "op": "ne", '
                  '"value": "example.com"}, {"field": "attributes.plan", "op": "in", '
                  '"value": ["pro", "team"]}]}',
    )  # fmt: skip

    def clean_definition(self) -> dict[str, Any]:
        from contacts import segments

        data = _json_object(self.cleaned_data["definition"], "Rules")
        segments.validate(data)  # a ValidationError lands on this field
        return data


class SuppressionForm(forms.Form):
    target = forms.CharField(label="Address or domain",
                             help_text="name@example.com, or *@example.com / example.com for a whole domain")  # fmt: skip
    scope = forms.ChoiceField(choices=[("MARKETING", "Marketing"), ("GLOBAL", "All mail")])
    reason = forms.ChoiceField(choices=[(Suppression.Reason.MANUAL, "Manual"),
                                        (Suppression.Reason.UNSUBSCRIBE, "Unsubscribe (by phone, email…)"),
                                        (Suppression.Reason.COMPLAINT, "Complaint")])  # fmt: skip
    source = forms.CharField(label="Why (kept with the record)", max_length=512)

    def clean_target(self) -> str:
        value = self.cleaned_data["target"].strip().lower()
        if value.startswith("*@"):
            value = value[2:]
        if not value or " " in value:
            raise forms.ValidationError("Enter one address or one domain.")
        return value


# ─── Rate limits and warm-up (§10.9 checks 6–7) ──────────────────────────

WINDOWS = [(1, "second"), (60, "minute"), (3600, "hour"), (86400, "day")]


def _domains(value: str) -> list[str]:
    return [d.strip() for d in value.replace("\n", ",").split(",") if d.strip()]


class RateLimitForm(forms.Form):
    """``target`` choices are "SCOPE:id" values supplied by the view."""

    scope = forms.ChoiceField(choices=[])
    target = forms.ChoiceField(choices=[], required=False, label="Applies to",
                               help_text="“Each one” counts every campaign, domain, … separately.")  # fmt: skip
    provider = forms.CharField(max_length=64, required=False, label="Provider name",
                               help_text="Provider limits only, e.g. gmail. Empty: each recipient domain.")  # fmt: skip
    provider_domains = forms.CharField(required=False, label="Provider's recipient domains",
                                       help_text="gmail.com, googlemail.com")  # fmt: skip
    max_messages = forms.IntegerField(min_value=1, label="Messages")
    per = forms.TypedChoiceField(choices=WINDOWS, coerce=int, initial=3600, label="Per")
    note = forms.CharField(max_length=300, required=False, label="Why (kept with the limit)")

    def __init__(self, *args: Any, targets: list[Any], **kwargs: Any) -> None:
        from delivery.models import RateLimit

        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["scope"]).choices = RateLimit.Scope.choices
        cast(forms.ChoiceField, self.fields["target"]).choices = [
            ("", "Each one / everything"),
            *targets,
        ]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        scope, target = data.get("scope", ""), data.get("target", "")
        if scope == "PROVIDER":
            data["target"] = data.get("provider", "").strip().lower()
            data["recipient_domains"] = _domains(data.get("provider_domains", ""))
            return data
        if target and target.partition(":")[0] != scope:
            raise forms.ValidationError(
                "Pick something of the same kind as the scope, or “Each one”."
            )
        data["target"] = target.partition(":")[2]
        data["recipient_domains"] = []
        return data


class WarmupPlanForm(forms.Form):
    target = forms.ChoiceField(choices=[], label="Domain or IP pool")
    started_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), label="Day 1")
    daily_limits = forms.CharField(label="Daily maximum, day by day",
                                   help_text="e.g. 50, 100, 200, 400, 800 — after the last day the plan is finished.")  # fmt: skip
    provider_domains = forms.CharField(required=False, label="Only mail to (optional)",
                                       help_text="Recipient domains, for a provider-specific ramp.")  # fmt: skip
    note = forms.CharField(max_length=300, required=False, label="Why (kept with the plan)")

    def __init__(self, *args: Any, targets: list[Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        cast(forms.ChoiceField, self.fields["target"]).choices = targets

    def clean_daily_limits(self) -> list[int]:
        parts = _domains(self.cleaned_data["daily_limits"])
        if not parts or not all(p.isdigit() and int(p) > 0 for p in parts):
            raise forms.ValidationError("Use positive whole numbers separated by commas.")
        return [int(p) for p in parts]

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        scope, _, target = data.get("target", "").partition(":")
        data.update(
            scope=scope, target=target, recipient_domains=_domains(data.get("provider_domains", ""))
        )
        return data
