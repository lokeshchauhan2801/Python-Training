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
            "id": str(pk),
            "title": title or "New session",
            "url": reverse("chat:session", args=[pk]),
        }
        for pk, title in sessions
    ]
    return {"admin_chat_history": history}
