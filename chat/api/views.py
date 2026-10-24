"""Views for the chat API."""

from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from basics.api.views import ScopedModelViewSet
from basics.llm import LLMConfigurationError

from ..models import ConversationSession
from ..services import ChatService
from .serializers import (
    AskSerializer,
    ConversationSerializer,
    ConversationSessionSerializer,
)


class ConversationSessionViewSet(ScopedModelViewSet):
    """List/create conversation sessions scoped to the request user.

    ``user``/audit are forced to the request scope by the base viewset;
    ``company`` is resolved by the serializer's required-company rule.
    """

    queryset = ConversationSession.objects.all()
    serializer_class = ConversationSessionSerializer


class AskView(APIView):
    """POST ``api/v1/chat/conversation/ask/`` -> run one chat turn.

    Resolves the session from company ownership (superuser: all), 404 if not in
    scope, then invokes ``ChatService.ask`` and returns the created turn.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from django.db.models import Q

        user = request.user
        sessions = (
            ConversationSession.objects.all()
            if user.is_superuser
            else ConversationSession.objects.filter(company__owner=user)
        )
        # Accept EITHER the primary key (`id`) OR the public `session_id`
        # reference, so a client can pass whichever ULID it copied from the
        # list response without hitting a confusing 404.
        session = get_object_or_404(
            sessions, Q(pk=data["session"]) | Q(session_id=data["session"])
        )

        try:
            turn = ChatService().ask(
                session=session, user=user, question=data["question"]
            )
        except LLMConfigurationError:
            return Response(
                {"error": "LLM not configured"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            ConversationSerializer(turn).data, status=status.HTTP_201_CREATED
        )
