"""Views for the global company switcher.

``set_active_company`` is the single setter for the session active company. It
is POST-only, login-required, and CSRF-protected (the default middleware
enforces CSRF — it is deliberately NOT exempted). It validates that the user may
access the posted company (owner-based: owned, or any active company for a
superuser) before storing it in the session, so a user can never switch to a
company they cannot access.
"""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from .active import allowed_companies_for_user


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


@login_required
@require_http_methods(["POST"])
def set_active_company(request):
    """Store the posted company id as the session active company.

    Validates the company against the user's accessible set. On success writes
    ``session["active_company_id"]`` and either returns JSON (AJAX) or redirects
    to a SAFE target (POSTed ``next`` → ``Referer`` → ``/``). On an unknown /
    inaccessible company id returns 403 (JSON when AJAX).
    """
    company_id = request.POST.get("company")
    company = (
        allowed_companies_for_user(request.user).filter(pk=company_id).first()
        if company_id
        else None
    )
    if company is None:
        if _is_ajax(request):
            return JsonResponse(
                {"error": "You cannot switch to that company."}, status=403
            )
        return HttpResponseForbidden("You cannot switch to that company.")

    request.session["active_company_id"] = str(company.pk)

    if _is_ajax(request):
        return JsonResponse({"active_company_id": str(company.pk), "ok": True})

    allowed_hosts = {request.get_host()}
    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts=allowed_hosts, require_https=request.is_secure()
    ):
        next_url = "/"
    return redirect(next_url)
