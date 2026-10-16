"""``chat_summarise`` tool — summarise the conversation's prior history.

Summarises the conversation's prior ``Message`` rows (NOT ``Conversation.turns``,
which is the wrong/empty history on the chat path). The message list is the
reused ``_SUMMARY_SYSTEM_PROMPT`` followed by the conversation context built via
``ChatService._build_conversation_context`` over the non-system ``Message`` rows
(ordered by ``created_at``), folded into one user turn. Calls
``LLMClient.complete`` and returns the summary.

All chat/LLM imports are lazy to keep ``agents.tools`` import-safe.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("chat_summarise")
def handle(ctx: ToolContext) -> ToolResult:
    from chat.models import Message
    from chat.services import ChatService, _SUMMARY_SYSTEM_PROMPT

    service = ChatService()
    history = list(
        ctx.conversation.messages.exclude(role=Message.ROLE_SYSTEM).order_by(
            "created_at"
        )
    )
    conversation_context = service._build_conversation_context(history)
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": conversation_context},
    ]
    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="chat_summarise")
