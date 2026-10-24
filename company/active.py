"""Session-based active-company resolution (global company switcher).

A faithful port of the OLD project's SESSION mechanism
(``company/access.py`` in the sibling django-unfold project) adapted to NEW's
OWNER-based tenancy. The active company is pure SESSION state
(``request.session["active_company_id"]``) — there is NO model/schema change and
NO migration.

Access model (owner-based, matching ``chat.scope.allowed_companies``):

* a superuser may access ALL active companies;
* everyone else may access the active companies they OWN.

Unlike OLD (which returns ``None`` to force an explicit choice when several
companies are available), NEW ALWAYS auto-selects a default when the user has at
least one accessible company, because the required NOT-NULL ``company`` FK on the
admin add form must never be left unresolved. The switcher lets the user change
which company is active; ``None`` is returned ONLY when the user has no
accessible company at all.

Every ``Company`` import is deferred INSIDE the functions so this module is safe
to import during app/settings wiring (same lazy pattern OLD uses).
"""


def allowed_companies_for_user(user):
    """Companies ``user`` may access, newest first.

    * unauthenticated → none;
    * superuser → all ACTIVE companies;
    * everyone else → the ACTIVE companies they own.

    Ordered ``-created_at, -id`` so the "default" (first) is the most-recently
    created company, matching the old ``_resolve_active_company`` behaviour.
    """
    from django.db.models import Q

    from company.models import Company

    if not getattr(user, "is_authenticated", False):
        return Company.objects.none()
    if user.is_superuser:
        return Company.objects.filter(is_active=True).order_by("-created_at", "-id")
    # Access = companies the user OWNS or is a MEMBER of (via CompanyMembership).
    return (
        Company.objects.filter(
            Q(owner=user) | Q(memberships__user=user),
            is_active=True,
        )
        .distinct()
        .order_by("-created_at", "-id")
    )


def can_switch_company(user):
    """True when the user can access more than one company (switcher shows)."""
    return allowed_companies_for_user(user).count() > 1


def active_company_for_request(request):
    """Resolve the company the current request operates within.

    Honours an explicit session selection (validated against the user's
    accessible companies). Pops an invalid selection. Auto-selects the default
    (most-recently created accessible company) and writes it back to the session
    when there is no valid selection yet. Returns ``None`` ONLY when the user has
    no accessible company.
    """
    user = getattr(request, "user", None)
    companies = allowed_companies_for_user(user)

    company_id = request.session.get("active_company_id")
    if company_id:
        company = companies.filter(pk=company_id).first()
        if company:
            return company

        request.session.pop("active_company_id", None)

    company = companies.first()
    if company is not None:
        request.session["active_company_id"] = str(company.pk)
        return company
    return None
