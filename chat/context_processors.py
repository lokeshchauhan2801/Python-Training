"""Context processors for the chat app.

``admin_chat_history`` feeds the Unfold sidebar a ChatGPT-style list of the
signed-in user's recent chat sessions (title + a link to the top-level
``/chat/<session_id>/`` route). Scope is re-derived server-side: a superuser
sees every non-deleted session, everyone else only their own owned-company
sessions — the same rule the ``/chat/`` views enforce.
"""

from django.urls import reverse

from .models import ConversationSession


def admin_chat_history(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"admin_chat_history": []}

    qs = ConversationSession.objects.all()
    if not user.is_superuser:
        qs = qs.filter(user=user, company__owner=user)

    sessions = qs.order_by("-updated_at").values_list("pk", "title")[:30]
    history = [
        {
            "title": title or "New session",
            "url": reverse("chat:session", args=[pk]),
        }
        for pk, title in sessions
    ]
    return {"admin_chat_history": history}
