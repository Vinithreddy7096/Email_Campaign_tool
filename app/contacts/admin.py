from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from typing import Any

from django.http import HttpRequest

from common.admin import NoDeleteAdmin, ViewOnlyAdmin
from contacts import services
from contacts.models import Contact, ContactImport, ContactImportFile, ContactList, Segment


@admin.register(Contact)
class ContactAdmin(NoDeleteAdmin):
    list_display = ("__str__", "first_name", "last_name", "created_at", "deleted_at")
    list_filter = ("deleted_at",)
    search_fields = ("email", "email_hash")
    readonly_fields = ("email_hash", "deleted_at")
    actions = ["erase"]

    @admin.action(description="Erase personal data (GDPR) — irreversible")
    def erase(self, request: HttpRequest, queryset: QuerySet[Contact]) -> None:
        for contact in queryset:
            contact.erase(by=request.user)


@admin.register(ContactList)
class ContactListAdmin(NoDeleteAdmin):
    list_display = ("name", "stream", "archived_at")
    list_filter = ("stream",)


@admin.register(ContactImport)
class ContactImportAdmin(ViewOnlyAdmin):
    list_display = ("original_filename", "contact_list", "status", "imported_count",
                    "attested_by", "created_at")  # fmt: skip
    list_filter = ("status",)


@admin.register(ContactImportFile)
class ContactImportFileAdmin(ViewOnlyAdmin):
    """Metadata only: the bytes are personal data and never shown here."""

    list_display = ("contact_import", "size", "sha256", "redacted_at", "created_at")
    exclude = ("content",)
    actions = ["redact"]

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # The redact action needs change permission; the change form has no
        # editable fields, so nothing else can be altered.
        return request.user.has_perm("contacts.change_contactimportfile")

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return ("contact_import", "size", "sha256", "redacted_at", "created_at", "updated_at")

    @admin.action(description="Redact the uploaded file (keeps size and checksum) — irreversible")
    def redact(self, request: HttpRequest, queryset: QuerySet[ContactImportFile]) -> None:
        for f in queryset:
            f.redact(by=request.user)


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    """Saves go through contacts.services so they are validated and audited."""

    list_display = ("name", "contact_list", "updated_at")
    list_select_related = ("contact_list",)

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> Any:
        return ("contact_list",) if obj is not None else ()

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        data = form.cleaned_data
        if change:
            services.update_segment(request.user, obj, name=data["name"],
                                    definition=data["definition"])  # fmt: skip
        else:
            created = services.create_segment(request.user, contact_list=data["contact_list"],
                                              name=data["name"], definition=data["definition"])  # fmt: skip
            obj.pk = created.pk
