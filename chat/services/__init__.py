"""chat.services: the one chat service and what sits on top of it.

* ``chat_service.ChatService`` - normal RAG chat (saves every turn).
* ``agent/`` - the Agent runtime (LangGraph): ``AgentChatService`` runs an
  Agent's graph JSON and uses ``ChatService`` underneath.
* ``router`` - picks one of the two from the send URL
  (``/send/`` = RAG, ``/send/<agent_slug>/`` = that Agent).

This __init__ re-exports the public surface of ``chat_service``, including the
``document_services`` binding, so ``chat.services.document_services is
documents.services`` stays true and every existing import keeps working.
"""

from .chat_service import (
    ChatService,
    ChatCompletion,
    ChatProviderError,
    ChatRetrievalError,
    ChartData,
    ChartPoint,
    _SUMMARY_SYSTEM_PROMPT,
    document_services,
)

__all__ = [
    "ChatService",
    "ChatCompletion",
    "ChatProviderError",
    "ChatRetrievalError",
    "ChartData",
    "ChartPoint",
    "_SUMMARY_SYSTEM_PROMPT",
    "document_services",
]
