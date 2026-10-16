"""``general_assistant`` tool — a plain LLM answer with no retrieval.

The default / direct-answer handler. It assembles the provider-neutral message
list from the conversation's prior ``Message`` history (excluding ``system``
rows, exactly as the RAG path does) plus the current user turn, then calls
``LLMClient.complete`` and returns the completion text.

All chat/LLM imports are lazy (inside the function) to avoid import cycles and
keep ``agents.tools`` import-safe.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("general_assistant")
def handle(ctx: ToolContext) -> ToolResult:
    from chat.models import Message

    history = (
        ctx.conversation.messages.exclude(role=Message.ROLE_SYSTEM)
        .order_by("created_at")
    )
    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": ctx.message_text})

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="general_assistant")
