from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from domains.models import IpPool, SenderIdentity, SendingDomain, SendingIp


@admin.register(IpPool)
class IpPoolAdmin(admin.ModelAdmin):
    list_display = ("name", "stream", "is_active")


@admin.register(SendingIp)
class SendingIpAdmin(admin.ModelAdmin):
    list_display = ("address", "ptr_hostname", "pool", "status")
    list_filter = ("status", "pool")


@admin.register(SendingDomain)
class SendingDomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "stream", "status", "dns_verified")
    list_filter = ("stream", "status")
    search_fields = ("domain",)
    # Verification timestamps are written by the DNS checker, not typed in.
    readonly_fields = (
        "spf_verified_at",
        "dkim_verified_at",
        "dmarc_verified_at",
        "last_dns_check_at",
    )

    actions = ["run_dns_check"]

    @admin.display(boolean=True, description="DNS verified")
    def dns_verified(self, obj: SendingDomain) -> bool:
        return obj.dns_verified

    @admin.action(description="Run DNS check now")
    def run_dns_check(self, request: HttpRequest, queryset: QuerySet[SendingDomain]) -> None:
        from workers.services.dns_checks import Status, dnspython_resolver
        from workers.services.domain_verification import verify_domain

        resolve = dnspython_resolver()
        for domain in queryset:
            report = verify_domain(domain, resolve, actor=request.user)
            failing = [f"{r.check}: {r.detail}" for r in report.results if not r.status.acceptable]
            if not failing:
                self.message_user(request, f"{domain}: all DNS checks passed", messages.SUCCESS)
            else:
                level = (
                    messages.WARNING
                    if all(
                        r.status is Status.UNKNOWN
                        for r in report.results
                        if not r.status.acceptable
                    )
                    else messages.ERROR
                )
                self.message_user(request, f"{domain}: " + " · ".join(failing), level)


@admin.register(SenderIdentity)
class SenderIdentityAdmin(admin.ModelAdmin):
    list_display = ("display_name", "from_address", "is_active")
    list_select_related = ("domain",)
