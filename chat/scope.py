"""Shared server-side scope helpers for the chat surfaces.

Both the in-admin chat scaffold (``chat.admin.ConversationSessionAdmin``) and the
top-level ``/chat/`` views (``chat.views``) must re-derive what a request may see
on the server, never trusting a client-supplied company/session id. These helpers
own that single rule so the two surfaces cannot drift:

* ``allowed_companies`` — the companies the user may use (superuser: all).
* ``scoped_sessions`` — the non-deleted sessions the user may see, newest first.

A superuser sees every non-deleted session; everyone else only their own
sessions limited to companies they own.
"""

from company.models import Company

from .models import ConversationSession


def allowed_companies(request):
    """Companies the requesting user may use (non-deleted only)."""
    if request.user.is_superuser:
        return Company.objects.all()
    return Company.objects.filter(owner=request.user)


def scoped_sessions(request):
    """Non-deleted sessions visible to the requesting user, newest first.

    Uses the non-deleted ``objects`` manager (the user-facing list), NOT the
    changelist's soft-delete-inclusive queryset.
    """
    qs = ConversationSession.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(
            user=request.user,
            company__in=allowed_companies(request),
        )
    return qs.order_by("-created_at")
