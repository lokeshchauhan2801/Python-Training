"""Context processor feeding the global company switcher to every template.

Registered in ``base.settings`` so EVERY admin page AND the ``/chat/`` page
carry the active company, the accessible set, whether the switcher may show, and
the setter URL. Guards for anonymous requests (login page) so it never 500s.
"""

from django.urls import reverse

from .active import (
    active_company_for_request,
    allowed_companies_for_user,
    can_switch_company,
)


def active_company(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "active_company": None,
            "accessible_companies": [],
            "can_switch_company": False,
            "set_active_company_url": reverse("company:set_active_company"),
        }
    return {
        "active_company": active_company_for_request(request),

        "accessible_companies": list(allowed_companies_for_user(user)),
        "can_switch_company": can_switch_company(user),
        "set_active_company_url": reverse("company:set_active_company"),
    }
