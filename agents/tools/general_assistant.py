"""``general_assistant`` tool — a plain LLM answer, no retrieval."""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("general_assistant")
def handle(ctx: ToolContext) -> ToolResult:
    # Lazy imports keep agents.tools import-safe and cycle-free.
    from chat.models import Message

    # Exclude system rows, exactly as the RAG path does.
    history = (
        ctx.conversation.messages.exclude(role=Message.ROLE_SYSTEM)
        .order_by("created_at")
    )
    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": ctx.message_text})

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="general_assistant")
