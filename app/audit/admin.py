from django.contrib import admin

from audit.models import AuditEvent
from common.admin import ViewOnlyAdmin


@admin.register(AuditEvent)
class AuditEventAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "action", "actor", "actor_label", "target_type", "target_id")
    list_filter = ("action",)
    search_fields = ("action", "target_id")
    date_hierarchy = "created_at"
