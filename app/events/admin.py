from django.contrib import admin

from common.admin import ViewOnlyAdmin
from events.models import DeliveryEvent, RawPayload


@admin.register(DeliveryEvent)
class DeliveryEventAdmin(ViewOnlyAdmin):
    list_display = ("occurred_at", "type", "source", "provider", "enhanced_code", "campaign")
    list_filter = ("type", "source", "provider")
    search_fields = ("email_hash", "enhanced_code", "source_key")
    date_hierarchy = "occurred_at"


@admin.register(RawPayload)
class RawPayloadAdmin(ViewOnlyAdmin):
    list_display = ("created_at", "source", "external_id", "processed_at", "error")
    list_filter = ("source",)
