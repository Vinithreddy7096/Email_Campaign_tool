from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import QuerySet
from django.http import HttpRequest

from accounts.models import ApiToken, Role, User, UserRole
from common.admin import ViewOnlyAdmin


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = ("email", "is_active", "is_staff", "mfa_enabled", "last_login")
    search_fields = ("email", "first_name", "last_name")
    # mfa_enabled is read-only: turning it on without an enrolled secret would
    # lock the user out. Users enrol themselves; admins can only reset.
    readonly_fields = (
        "last_login",
        "date_joined",
        "failed_login_count",
        "last_login_ip",
        "mfa_enabled",
    )
    actions = ["reset_mfa"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name")}),
        ("Access", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Security", {"fields": ("mfa_enabled", "locked_until", "failed_login_count")}),
        ("History", {"fields": ("last_login", "last_login_ip", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)

    @admin.action(description="Reset MFA (lost device) — user re-enrols at next login")
    def reset_mfa(self, request: HttpRequest, queryset: QuerySet[User]) -> None:
        for user in queryset:
            user.reset_mfa(by=request.user)  # type: ignore[arg-type]

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        obj.username = obj.email
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Users are referenced by audit and consent rows; deactivate instead.
        return False


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code", "description")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # the role set is fixed by migration

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    """Grants and revocations go through UserRole.grant/revoke so they are audited."""

    list_display = ("user", "role", "granted_by", "created_at")
    list_filter = ("role",)
    fields = ("user", "role")

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def save_model(self, request: HttpRequest, obj: Any, form: Any, change: bool) -> None:
        row, _ = UserRole.grant(obj.user, obj.role.code, by=request.user)  # type: ignore[arg-type]
        obj.pk = row.pk

    def delete_model(self, request: HttpRequest, obj: Any) -> None:
        UserRole.revoke(obj.user, obj.role.code, by=request.user)  # type: ignore[arg-type]

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[Any]) -> None:
        for obj in queryset.select_related("user", "role"):
            self.delete_model(request, obj)


@admin.register(ApiToken)
class ApiTokenAdmin(ViewOnlyAdmin):
    """Tokens are minted by ``manage.py issue_api_token``, which shows the secret
    once. The admin can list and revoke them, never mint or reveal them."""

    list_display = ("name", "user", "prefix", "expires_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    exclude = ("secret_hash",)
    actions = ["revoke"]

    @admin.action(description="Revoke selected tokens")
    def revoke(self, request: HttpRequest, queryset: QuerySet[ApiToken]) -> None:
        for token in queryset:
            token.revoke(by=request.user)  # type: ignore[arg-type]

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Required for the revoke action to appear; every field stays read-only.
        return True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        return [f.name for f in ApiToken._meta.fields if f.name != "secret_hash"]
