"""Reusable admin building blocks.

Three mixins plus ``BaseModelAdmin``. The MRO on ``BaseModelAdmin`` is pinned so
the ``all_objects`` swap runs first, then the superuser early-return, then the
company scope filter:

    CompanyScopedAdminMixin -> AuditAdminMixin -> SoftDeleteAdminMixin -> unfold.ModelAdmin
"""

from django.core.exceptions import PermissionDenied
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
    """Scope the changelist to the owner/company of the requesting user.

    HOUSE RULE: the ``company`` FK is never a free picker for a non-superuser.
    In the add/change form its queryset is limited to companies the user owns
    and defaulted to the resolved active/single-owned company (the SAME rule as
    ``chat.api.serializers.RequiredCompanyResolutionMixin._resolve_company``);
    ``save_model`` re-scopes/rejects server-side regardless of the submitted
    form. Superusers keep the full queryset and may pick across all companies.
    """

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

    def _owned_companies(self, request):
        """Companies the requesting user may PICK from in the form.

        Superusers keep the full queryset (they may choose any company in the
        change form); everyone else is limited to companies they own. This is
        the picker scope, NOT the auto-fill default — the default is resolved by
        ``_resolve_active_company`` from the companies the user actually OWNS.
        """
        from company.models import Company

        if request.user.is_superuser:
            return Company.objects.all()
        return Company.objects.filter(owner=request.user)

    def _resolve_active_company(self, request):
        """Resolve the user's active/default owned company for server-side fill.

        Reuses the project's owner-based "active company" rule (the same
        ``company__owner=request.user`` ownership the chat surfaces use — see
        ``chat.scope.allowed_companies`` and
        ``chat.api.serializers.RequiredCompanyResolutionMixin``):

        * exactly one owned company → that company;
        * several owned companies, no explicit selection → a deterministic
          default (the most-recently created owned company) rather than erroring,
          so a required ``company`` is never left null;
        * no owned company → ``None`` (the caller surfaces a clean admin error).

        This resolves by OWNERSHIP for every user, including superusers, so a
        superuser who owns a company gets it auto-filled when the hidden-company
        add form submits no company.
        """
        from company.models import Company

        owned = Company.objects.filter(owner=request.user).order_by(
            "-created_at", "-id"
        )
        return owned.first()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "company" and not request.user.is_superuser:
            kwargs["queryset"] = self._owned_companies(request)
            active = self._resolve_active_company(request)
            if active is not None:
                kwargs["initial"] = active.pk
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _enforce_company(self, request, obj):
        """Server-side company enforcement, independent of the submitted form.

        The required ``company`` FK is NEVER trusted to the submitted form (it is
        hidden from the minimal add form, so a POST carries no company at all).
        This stamps it server-side so the NOT NULL constraint is always
        satisfied and a null company can never reach the INSERT:

        * company already set and allowed → keep it;
        * company unset → auto-fill the resolved active/default owned company;
        * a non-superuser's submitted company they do not own → coerce to the
          owned default;
        * no resolvable company → a clean, user-facing admin error instead of a
          500 IntegrityError.
        """
        model_field_names = {f.name for f in obj._meta.get_fields()}
        if "company" not in model_field_names:
            return
        active = self._resolve_active_company(request)
        if not obj.company_id:
            # No company on the row (hidden add form / tampered POST): stamp the
            # resolved owned default. Applies to superusers too, so a hidden
            # company field never leaves company_id null.
            if active is None:
                raise PermissionDenied(
                    "You must have a company before adding a document."
                )
            obj.company = active
            return
        # Superusers may deliberately pick any company in the change form; honor
        # a company that is already set for them.
        if request.user.is_superuser:
            return
        # A non-superuser submitted a company they do not own: coerce to the
        # owned default when there is one, else reject.
        if not self._owned_companies(request).filter(pk=obj.company_id).exists():
            if active is None:
                raise PermissionDenied(
                    "You must have a company before adding a document."
                )
            obj.company = active

    def save_model(self, request, obj, form, change):
        # Enforce the company BEFORE the row is written (and before audit
        # stamping in AuditAdminMixin.save_model) so a tampered POST can never
        # persist another owner's company.
        self._enforce_company(request, obj)
        super().save_model(request, obj, form, change)

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
    # REUSABLE house rule: a subclass may name an "actor" FK (e.g.
    # DocumentAdmin.actor_field = "uploaded_by"). When set, that field is made
    # read-only (never a selectable dropdown) and auto-stamped to request.user
    # on create — the user never hand-picks who uploaded/owns a row.
    actor_field = None

    actions = ["soft_delete_selected", "restore_selected"]

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if self.actor_field and self.actor_field not in readonly:
            readonly.append(self.actor_field)
        return readonly

    def save_model(self, request, obj, form, change):
        # Force the actor FK to request.user on create (before the row is
        # written). The field is read-only so it is never in the submitted
        # form; forcing (rather than defaulting when unset) also coerces any
        # tampered value back to the acting user.
        if self.actor_field and not change:
            setattr(obj, self.actor_field, request.user)
        super().save_model(request, obj, form, change)
