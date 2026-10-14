"""URLConf for the chat API, mounted at ``api/v1/chat/``."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import AskView, ConversationSessionViewSet

router = SimpleRouter(trailing_slash=True)
router.register(
    "conversation-session", ConversationSessionViewSet, basename="conversation-session"
)

urlpatterns = [
    path("conversation/ask/", AskView.as_view(), name="chat-conversation-ask"),
    *router.urls,
]
