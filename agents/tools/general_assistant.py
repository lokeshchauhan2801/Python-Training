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

    # Optional Langfuse system prompt; empty fallback => behave exactly as
    # before (no system message prepended).
    name = (ctx.tool.prompt_name if ctx.tool else "") or "tool_general_assistant"
    sys = ctx.langfuse.get_prompt(name, fallback="") if ctx.langfuse else ""
    if sys.strip():
        messages.insert(0, {"role": "system", "content": sys})

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="general_assistant")
