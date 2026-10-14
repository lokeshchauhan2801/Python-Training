"""Admin registration for ``company.Company``.

``CompanyAdmin`` uses the reusable ``BaseModelAdmin`` so it gets soft-delete
visibility, owner scoping, and the shared audit stamping. The audit fields are
made read-only by ``AuditAdminMixin.get_readonly_fields`` and are grouped into a
dedicated fieldset here.
"""

from django.contrib import admin

from basics.admin import BaseModelAdmin

from .models import Company


@admin.register(Company)
class CompanyAdmin(BaseModelAdmin):
    list_display = ("name", "slug", "owner", "is_active", "is_deleted", "created_at")
    list_filter = ("is_active", "is_deleted", "created_at")
    search_fields = ("name", "slug", "owner__email")
    date_hierarchy = "created_at"
    prepopulated_fields = {"slug": ("name",)}

    fieldsets = (
        (None, {"fields": ("id", "name", "slug", "description")}),
        ("Ownership", {"fields": ("owner", "is_active")}),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "created_by",
                    "updated_by",
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
    )
