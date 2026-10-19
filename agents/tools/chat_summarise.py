"""``chat_summarise`` tool — summarise the conversation's prior history."""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("chat_summarise")
def handle(ctx: ToolContext) -> ToolResult:
    # Lazy imports keep agents.tools import-safe. Summarise the Message rows
    # (not Conversation.turns, which is empty on the chat path).
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
