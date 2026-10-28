"""Admin registration for the Agents app (BaseModelAdmin, documents house style)."""

from django.contrib import admin

from basics.admin import BaseModelAdmin

from .models import Agent, Tool


@admin.register(Tool)
class ToolAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "name",
        "slug",
        "type",
        "is_enabled",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("type", "is_enabled", "company", "is_deleted", "created_at")
    search_fields = ("name", "slug", "description")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(Agent)
class AgentAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "slug",
        "name",
        "connected_from_start",
        "connected_to_end",
        "is_active",
        "order",
        "company",
        "created_at",
    )
    list_filter = (
        "connected_from_start",
        "connected_to_end",
        "is_active",
        "company",
        "is_deleted",
        "created_at",
    )
    search_fields = ("slug", "name")
    filter_horizontal = ("tools",)
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]
