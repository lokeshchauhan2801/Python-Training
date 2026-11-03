"""Keycloak (OIDC) login backend.

Security rules (each one closes a specific hole):

* Users are found ONLY by the Keycloak ``sub`` through ``OIDCIdentity`` —
  never by email — so a Keycloak account cannot log in as a local user that
  happens to share its email.
* ``email`` is this project's login field and is unique, so a new Keycloak
  account whose email already belongs to an unlinked user is REFUSED (link it
  first with ``link_keycloak_identities``).
* ``is_staff`` / ``is_superuser`` are set from the ``groups`` claim (full
  paths) on every login; a missing claim means no groups (fail closed).
* Logs never contain tokens or full claim payloads.
"""

import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY
from django.db import IntegrityError, transaction
from django.shortcuts import resolve_url
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

from .models import OIDCIdentity

logger = logging.getLogger(__name__)


def _groups(claims) -> set:
    groups = claims.get("groups")
    if not isinstance(groups, list):
        return set()
    return {g for g in groups if isinstance(g, str)}


class KeycloakOIDCBackend(OIDCAuthenticationBackend):
    def verify_claims(self, claims):
        sub = claims.get("sub")
        if not sub:
            logger.warning("Keycloak login refused: no sub claim")
            return False
        if claims.get("email_verified") is not True:
            logger.warning("Keycloak login refused: email not verified (sub=%s)", sub)
            return False
        if not claims.get("email"):
            logger.warning("Keycloak login refused: no email claim (sub=%s)", sub)
            return False
        return True

    def filter_users_by_claims(self, claims):
        sub = claims.get("sub")
        if not sub:
            return self.UserModel.objects.none()
        return self.UserModel.objects.filter(oidc_identity__sub=sub)

    def create_user(self, claims):
        sub = claims["sub"]
        email = self.UserModel.objects.normalize_email(claims["email"])
        username = f"kc-{sub}"
        # all_objects: soft-deleted users still own their email/username.
        existing = self.UserModel.all_objects
        if existing.filter(email__iexact=email).exists():
            logger.warning(
                "Keycloak login refused: email already belongs to a user not "
                "linked to this Keycloak account (sub=%s). Link it with "
                "link_keycloak_identities if it is the same person.",
                sub,
            )
            return None
        if existing.filter(username=username).exists():
            logger.warning("Keycloak login refused: username %s already taken", username)
            return None
        try:
            with transaction.atomic():
                user = self.UserModel.objects.create_user(
                    username=username,
                    email=email,
                    first_name=claims.get("given_name") or "",
                    last_name=claims.get("family_name") or "",
                )
                self._sync_roles(user, claims, created=True)
                user.save()
                OIDCIdentity.objects.create(user=user, sub=sub)
        except IntegrityError:
            logger.warning("Keycloak login refused: user or sub already exists (sub=%s)", sub)
            return None
        logger.info("Keycloak login: created user %s (sub=%s)", user.pk, sub)
        return user

    def update_user(self, user, claims):
        # Email is NOT updated from Keycloak: it is the local login field and
        # unique, and changing it here could collide with another account.
        fields = self._sync_roles(user, claims, created=False)
        for claim, attr in (("given_name", "first_name"), ("family_name", "last_name")):
            value = claims.get(claim)
            if value and getattr(user, attr) != value:
                setattr(user, attr, value)
                fields.append(attr)
        if fields:
            user.save(update_fields=fields)
        logger.info("Keycloak login: user %s (sub=%s)", user.pk, claims.get("sub"))
        return user

    def _sync_roles(self, user, claims, created):
        groups = _groups(claims)
        wanted = {
            "is_staff": settings.KEYCLOAK_ADMIN_GROUP in groups,
            "is_superuser": settings.KEYCLOAK_SUPERUSER_GROUP in groups,
        }
        changed = []
        for attr, new in wanted.items():
            old = getattr(user, attr)
            if old != new or created:
                setattr(user, attr, new)
                logger.info(
                    "Keycloak role sync: user %s %s %s -> %s",
                    user.pk,
                    attr,
                    None if created else old,
                    new,
                )
                if old != new:
                    changed.append(attr)
        return changed


def provider_logout(request):
    """Logout URL: also end the Keycloak session for Keycloak logins.

    Without this, the next /admin/login/ would silently log the user back in
    through Keycloak's still-open session. Break-glass (local) sessions just
    go to LOGOUT_REDIRECT_URL.
    """
    redirect_to = request.build_absolute_uri(resolve_url(settings.LOGOUT_REDIRECT_URL))
    backend = request.session.get(BACKEND_SESSION_KEY, "")
    if backend != f"{KeycloakOIDCBackend.__module__}.{KeycloakOIDCBackend.__name__}":
        return redirect_to
    params = {
        "client_id": settings.OIDC_RP_CLIENT_ID,
        "post_logout_redirect_uri": redirect_to,
    }
    id_token = request.session.get("oidc_id_token")
    if id_token:
        params["id_token_hint"] = id_token
    return f"{settings.OIDC_OP_LOGOUT_ENDPOINT}?{urlencode(params)}"
