from django.contrib import admin

from common.admin import ViewOnlyAdmin
from consent.models import ConsentRecord


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ViewOnlyAdmin):
    list_display = ("contact", "stream", "contact_list", "action", "method", "occurred_at")
    list_filter = ("stream", "action", "method")
    search_fields = ("email_hash",)
