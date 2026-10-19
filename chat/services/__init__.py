"""chat.services package — public surface re-exported from chat_service.

The chat orchestration service used to live in a single chat/services.py
module. It is now a package so the LangGraph runtime can sit beside it
(langgraph_service.py + graph_state.py). This __init__ re-exports the full
public surface byte-for-byte, including the ``document_services`` binding, so
``chat.services.document_services is documents.services`` stays true and every
existing import keeps working.
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
