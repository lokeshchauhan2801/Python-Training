"""Admin registration for ``accounts.User``.

``UserAdmin`` is a special case: it preserves Django's password handling and
add-user form (``DjangoUserAdmin.add_form`` / ``form``) and does NOT apply
company scoping (``User`` has no company FK). It mixes in ``SoftDeleteAdminMixin``
so soft-deleted users stay visible for restore (``User.all_objects`` is the
``AllUsersManager``), and ``AuditAdminMixin`` for read-only audit fields and the
shared actor-stamping ``save_model``.

MRO: AuditAdminMixin -> SoftDeleteAdminMixin -> UnfoldModelAdmin -> DjangoUserAdmin
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from unfold.admin import ModelAdmin as UnfoldModelAdmin

from basics.admin import AuditAdminMixin, SoftDeleteAdminMixin

from .models import User


@admin.register(User)
class UserAdmin(
    AuditAdminMixin,
    SoftDeleteAdminMixin,
    UnfoldModelAdmin,
    DjangoUserAdmin,
):
    list_display = (
        "email",
        "username",
        "first_name",
        "last_name",
        "is_staff",
        "is_active",
        "is_deleted",
        "date_joined",
    )
    list_filter = ("is_staff", "is_active", "is_superuser", "is_deleted")
    search_fields = ("email", "username", "first_name", "last_name")
    ordering = ("-date_joined",)
    date_hierarchy = "date_joined"

    fieldsets = (
        (None, {"fields": ("id", "email", "username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
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
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "username"),
            },
        ),
    )
