"""Admin registration for the Agents app (BaseModelAdmin, documents house style)."""

from django.contrib import admin
from django.db.models import Q

from basics.admin import BaseModelAdmin, CompanyScopedAdminMixin

from .models import Agent, Step, Tool


class GlobalRowsAdminMixin:
    """Agents, steps and tools without a company are global (every company
    uses them). Show them next to the active company's rows, keep them global
    on save, and let only a superuser change them.
    """

    def get_queryset(self, request):
        # Bypass the active-company filter, then re-apply it with globals added.
        qs = super(CompanyScopedAdminMixin, self).get_queryset(request)
        active = self._resolve_active_company(request)
        scope = Q(company__isnull=True)
        if active is not None:
            scope |= Q(company=active)
        return qs.filter(scope)

    def _enforce_company(self, request, obj):
        if isinstance(obj, Step) and obj.agent_id:
            obj.company = obj.agent.company  # a step follows its agent
            return
        if obj.company_id is None and request.user.is_superuser:
            return  # left empty by a superuser: global
        super()._enforce_company(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.company_id is None and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.company_id is None and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description="Company", ordering="company")
    def company_or_global(self, obj):
        return obj.company or "Global (all companies)"


@admin.register(Tool)
class ToolAdmin(GlobalRowsAdminMixin, BaseModelAdmin):
    list_display = (
        "id_short",
        "name",
        "slug",
        "is_enabled",
        "company_or_global",
        "created_by",
        "created_at",
    )
    list_filter = ("is_enabled", "company", "is_deleted", "created_at")
    search_fields = ("name", "slug", "description")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(Agent)
class AgentAdmin(GlobalRowsAdminMixin, BaseModelAdmin):
    list_display = (
        "id_short",
        "slug",
        "route",
        "name",
        "company_or_global",
        "created_at",
    )
    list_filter = (
        "company",
        "is_deleted",
        "created_at",
    )
    search_fields = ("slug", "route", "name")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]


@admin.register(Step)
class StepAdmin(GlobalRowsAdminMixin, BaseModelAdmin):
    list_display = (
        "id_short",
        "slug",
        "name",
        "agent",
        "connected_from_start",
        "connected_to_end",
        "order",
        "company_or_global",
        "created_at",
    )
    list_filter = (
        "connected_from_start",
        "connected_to_end",
        "company",
        "is_deleted",
        "created_at",
    )
    search_fields = ("slug", "name")
    filter_horizontal = ("tools",)
    raw_id_fields = ("agent", "next_step")
    date_hierarchy = "created_at"

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]
