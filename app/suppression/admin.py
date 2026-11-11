from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin
from django.http import HttpRequest

from common.admin import NoDeleteAdmin
from suppression.models import Suppression


class SuppressionForm(forms.ModelForm):
    class Meta:
        model = Suppression
        fields = ("email", "domain", "scope", "stream", "contact_list", "reason", "source")

    def clean(self) -> dict[str, Any]:
        data = super().clean() or {}
        if bool(data.get("email")) == bool(data.get("domain")):
            raise forms.ValidationError("Enter either an email address or a domain, not both.")
        return data


@admin.register(Suppression)
class SuppressionAdmin(NoDeleteAdmin):
    """Staff can add a manual suppression. Existing rows are read-only here:
    lifting goes through Suppression.lift(), which requires a reason and audits."""

    list_display = ("__str__", "scope", "reason", "created_at", "lifted_at")
    list_filter = ("scope", "reason", "stream")
    search_fields = ("email", "email_hash", "domain")
    form = SuppressionForm
    # Fill in either email or domain, not both (enforced by a DB constraint).
    fields = ("email", "domain", "scope", "stream", "contact_list", "reason", "source")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        if obj is None:
            return ()
        return (*self.fields, "created_by", "lifted_at", "lifted_by", "lift_reason")

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
