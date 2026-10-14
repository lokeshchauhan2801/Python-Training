"""Reusable admin building blocks.

Three mixins plus ``BaseModelAdmin``. The MRO on ``BaseModelAdmin`` is pinned so
the ``all_objects`` swap runs first, then the superuser early-return, then the
company scope filter:

    CompanyScopedAdminMixin -> AuditAdminMixin -> SoftDeleteAdminMixin -> unfold.ModelAdmin
"""

from unfold.admin import ModelAdmin as UnfoldModelAdmin

# Audit fields that may be made read-only when present on a model.
AUDIT_FIELD_NAMES = (
    "id",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    "is_deleted",
    "deleted_at",
)


class SoftDeleteAdminMixin:
    """Make soft-deleted rows visible in the changelist (so restore works)."""

    def get_queryset(self, request):
        manager = getattr(self.model, "all_objects", self.model._default_manager)
        qs = manager.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs


class AuditAdminMixin:
    """Make audit fields read-only and stamp the acting admin on save."""

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        model_field_names = {f.name for f in self.model._meta.get_fields()}
        for name in AUDIT_FIELD_NAMES:
            if name in model_field_names and name not in readonly:
                readonly.append(name)
        return readonly

    def save_model(self, request, obj, form, change):
        # Run the superclass first so password hashing (DjangoUserAdmin /
        # UserCreationForm) and any form.save side effects happen, and a PK is
        # assigned, BEFORE we stamp the audit columns.
        super().save_model(request, obj, form, change)
        # MEDIUM FIX 1: compute update_fields dynamically; never obj.save(actor=...).
        fields = []
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
            fields.append("created_by")
        obj.updated_by = request.user
        fields += ["updated_by", "updated_at"]
        obj.save(update_fields=fields)


class CompanyScopedAdminMixin:
    """Scope the changelist to the owner/company of the requesting user."""

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        model_field_names = {f.name for f in self.model._meta.get_fields()}
        if "company" in model_field_names:
            return qs.filter(company__owner=request.user)
        if "owner" in model_field_names:
            return qs.filter(owner=request.user)
        return qs

    # --- actor-aware soft delete / restore ---------------------------------
    def delete_model(self, request, obj):
        obj.delete(actor=request.user)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj.delete(actor=request.user)

    def soft_delete_selected(self, request, queryset):
        for obj in queryset:
            obj.delete(actor=request.user)

    soft_delete_selected.short_description = "Soft delete selected"

    def restore_selected(self, request, queryset):
        for obj in queryset:
            obj.restore(actor=request.user)

    restore_selected.short_description = "Restore selected"


class BaseModelAdmin(
    CompanyScopedAdminMixin,
    AuditAdminMixin,
    SoftDeleteAdminMixin,
    UnfoldModelAdmin,
):
    actions = ["soft_delete_selected", "restore_selected"]
