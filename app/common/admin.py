"""Admin base classes that mirror the data model's immutability rules."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest


class ViewOnlyAdmin(admin.ModelAdmin):
    """For append-only tables: rows are written by the application, never here."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class NoDeleteAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False
