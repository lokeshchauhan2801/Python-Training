"""Admin registration for the Agents app (BaseModelAdmin, documents house style)."""

from django.contrib import admin

from basics.admin import BaseModelAdmin

from .models import AgentEdge, AgentGraph, AgentNode, Tool


@admin.register(AgentGraph)
class AgentGraphAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "name",
        "is_active",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("is_active", "company", "is_deleted", "created_at")
    search_fields = ("name", "description")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(Tool)
class ToolAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "name",
        "slug",
        "is_enabled",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("is_enabled", "slug", "company", "is_deleted", "created_at")
    search_fields = ("name", "slug", "description")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(AgentNode)
class AgentNodeAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "slug",
        "kind",
        "graph",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("kind", "graph", "company", "is_deleted", "created_at")
    search_fields = ("slug", "label", "prompt_name")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(AgentEdge)
class AgentEdgeAdmin(BaseModelAdmin):
    list_display = (
        "id_short",
        "graph",
        "source",
        "target",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("graph", "company", "is_deleted", "created_at")
    search_fields = ("condition",)
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]
