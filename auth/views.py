"""Admin login/logout routing for Keycloak, plus the break-glass local login.

With ``KEYCLOAK_LOGIN_ENABLED`` off, /admin/login/ and /admin/logout/ are the
stock Django admin views, unchanged. With it on, /admin/login/ hands off to
Keycloak (keeping ``?next=``) and /admin/logout/ also ends the Keycloak
session. /admin/local-login/ exists only while ``BREAK_GLASS_LOGIN_ENABLED``
is on and only accepts accounts NOT linked to Keycloak.
"""

import logging
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.views import LoginView
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from mozilla_django_oidc.views import OIDCLogoutView

from .models import OIDCIdentity

logger = logging.getLogger(__name__)


def _safe_next(request):
    next_url = request.GET.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return next_url
    return ""


def admin_login(request, extra_context=None):
    if not settings.KEYCLOAK_LOGIN_ENABLED:
        return admin.site.login(request, extra_context)
    user = request.user
    if user.is_authenticated:
        if user.is_active and user.is_staff:
            return redirect(_safe_next(request) or reverse("admin:index"))
        # Logged in through Keycloak but not an admin: sending them back to
        # Keycloak would just log them in again and loop here.
        return render(request, "auth/login_problem.html", {"reason": "no_access"}, status=403)
    url = reverse("oidc_authentication_init")
    next_url = _safe_next(request)
    if next_url:
        url = f"{url}?{urlencode({'next': next_url})}"
    return redirect(url)


def admin_logout(request, extra_context=None):
    if not settings.KEYCLOAK_LOGIN_ENABLED:
        return admin.site.logout(request, extra_context)
    return OIDCLogoutView.as_view()(request)


def login_failed(request):
    keycloak_logout = f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?" + urlencode(
        {
            "client_id": settings.OIDC_RP_CLIENT_ID,
            "post_logout_redirect_uri": request.build_absolute_uri(
                settings.LOGOUT_REDIRECT_URL
            ),
        }
    )
    return render(
        request,
        "auth/login_problem.html",
        {"reason": "login_failed", "keycloak_logout_url": keycloak_logout},
        status=403,
    )


class BreakGlassAuthenticationForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if OIDCIdentity.objects.filter(user=user).exists():
            raise forms.ValidationError(
                "This account is linked to Keycloak and cannot use break-glass login.",
                code="linked",
            )


class LocalLoginView(LoginView):
    """Break-glass password login, only while BREAK_GLASS_LOGIN_ENABLED is on."""

    authentication_form = BreakGlassAuthenticationForm
    template_name = "admin/login.html"
    redirect_authenticated_user = False

    def dispatch(self, request, *args, **kwargs):
        if not settings.BREAK_GLASS_LOGIN_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(admin.site.each_context(self.request))
        context.update(title="Break-glass login", app_path=self.request.get_full_path())
        return context

    def get_default_redirect_url(self):
        return reverse("admin:index")

    def form_valid(self, form):
        logger.warning("Break-glass login: user %s", form.get_user().pk)
        return super().form_valid(form)

    def form_invalid(self, form):
        logger.warning("Break-glass login failed")
        return super().form_invalid(form)
