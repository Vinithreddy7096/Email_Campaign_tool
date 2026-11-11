"""Admin screens for templates (BUILD.md §10.5.4)."""

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import audit, preflight, services
from .models import EmailTemplate, TemplateVersion
from .rendering import RenderError, render

Status = TemplateVersion.Status
_SERVICE_ERRORS = (
    services.InvalidTransition,
    services.PreflightFailed,
    PermissionDenied,
    ValidationError,
)


class TemplateVersionInline(admin.TabularInline):
    model = TemplateVersion
    fields = ("version_link", "status", "subject", "created_by", "created_at")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Version")
    def version_link(self, obj):
        url = reverse("admin:email_templates_templateversion_change", args=[obj.pk])
        return format_html('<a href="{}">v{}</a>', url, obj.number)


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "owner", "latest_approved", "archived_at")
    list_filter = ("category",)
    search_fields = ("name", "description")
    readonly_fields = ("owner", "created_at", "updated_at")
    inlines = [TemplateVersionInline]

    @admin.display(description="Latest approved")
    def latest_approved(self, obj):
        version = obj.latest_approved_version()
        return f"v{version.number}" if version else "-"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.owner = request.user
        super().save_model(request, obj, form, change)
        audit.record(
            actor=request.user,
            action="email_template.edit" if change else "email_template.create",
            obj=obj,
            after={f: form.cleaned_data.get(f) for f in form.changed_data},
        )

    def has_delete_permission(self, request, obj=None):
        return False  # archive instead; versions may be referenced by sends


@admin.register(TemplateVersion)
class TemplateVersionAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "template",
        "number",
        "status",
        "created_by",
        "created_at",
        "reviewed_by",
    )
    list_filter = ("status", "template__category")
    search_fields = ("template__name", "subject")
    list_select_related = ("template", "created_by", "reviewed_by")
    actions = ["submit_for_approval", "new_version", "retire_versions"]
    readonly_fields = (
        "review_link",
        "number",
        "status",
        "checksum",
        "created_by",
        "created_at",
        "submitted_by",
        "submitted_at",
        "reviewed_by",
        "reviewed_at",
        "review_note",
    )
    fields = (
        "template",
        "subject",
        "html_body",
        "text_body",
        "variables",
        "sample_data",
    ) + readonly_fields

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        if obj.is_editable:
            return ("template",) + self.readonly_fields
        return ("template",) + TemplateVersion.CONTENT_FIELDS + self.readonly_fields

    def has_delete_permission(self, request, obj=None):
        return obj is not None and obj.is_editable and super().has_delete_permission(request, obj)

    def has_submit_permission(self, request):
        return request.user.has_perm("email_templates.submit_templateversion")

    def has_approve_permission(self, request):
        return request.user.has_perm("email_templates.approve_templateversion")

    def save_model(self, request, obj, form, change):
        services.save_draft(request.user, obj)

    @admin.display(description="Preview and review")
    def review_link(self, obj):
        if obj is None or obj.pk is None:
            return "-"
        url = reverse("admin:email_templates_templateversion_review", args=[obj.pk])
        return format_html('<a href="{}">Open preview, preflight and review</a>', url)

    def _run_action(self, request, queryset, func, done):
        for version in queryset:
            try:
                func(request.user, version)
            except _SERVICE_ERRORS as exc:
                self.message_user(request, f"{version}: {exc}", messages.ERROR)
            else:
                self.message_user(request, f"{version}: {done}", messages.SUCCESS)

    @admin.action(description="Submit for approval (runs preflight)", permissions=["submit"])
    def submit_for_approval(self, request, queryset):
        self._run_action(request, queryset, services.submit, "submitted for approval.")

    @admin.action(description="Create a new draft version from this one", permissions=["add"])
    def new_version(self, request, queryset):
        self._run_action(request, queryset, services.new_version_from, "new draft created.")

    @admin.action(description="Retire (stop new use)", permissions=["approve"])
    def retire_versions(self, request, queryset):
        self._run_action(request, queryset, services.retire, "retired.")

    def get_urls(self):
        return [
            path(
                "<int:object_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="email_templates_templateversion_review",
            ),
        ] + super().get_urls()

    def review_view(self, request, object_id):
        version = get_object_or_404(
            TemplateVersion.objects.select_related("template"), pk=object_id
        )
        if not self.has_view_or_change_permission(request, version):
            raise PermissionDenied
        can_review = version.status == Status.PENDING_APPROVAL and self.has_approve_permission(
            request
        )

        if request.method == "POST":
            if not can_review:
                raise PermissionDenied
            decision = request.POST.get("decision")
            note = request.POST.get("note", "")
            func = {
                "approve": services.approve,
                "reject": services.reject,
                "request_changes": services.request_changes,
            }.get(decision)
            if func is None:
                self.message_user(request, "Unknown decision.", messages.ERROR)
            else:
                try:
                    func(request.user, version, note)
                except _SERVICE_ERRORS as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    self.message_user(
                        request,
                        f"{version}: {decision.replace('_', ' ')} recorded.",
                        messages.SUCCESS,
                    )
            return redirect("admin:email_templates_templateversion_review", object_id=version.pk)

        findings = preflight.run(version)
        try:
            rendered = render(
                version,
                version.sample_data,
                preflight.preview_system_data(),
                require_approved=False,
            )
            render_error = None
        except RenderError as exc:
            rendered, render_error = None, str(exc)
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Review {version}",
            "version": version,
            "rendered": rendered,
            "render_error": render_error,
            "findings": findings,
            "has_failures": preflight.has_failures(findings),
            "can_review": can_review,
        }
        return TemplateResponse(
            request, "admin/email_templates/templateversion/review.html", context
        )
