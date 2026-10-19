"""``prompt_tool`` — generic prompt-only tool driven by a Langfuse prompt."""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("prompt_tool")
def handle(ctx: ToolContext) -> ToolResult:
    # Lazy imports keep agents.tools import-safe and cycle-free.
    from chat.models import Message

    # Same history + current turn as general_assistant.
    history = (
        ctx.conversation.messages.exclude(role=Message.ROLE_SYSTEM)
        .order_by("created_at")
    )
    messages = [{"role": m.role, "content": m.content} for m in history]
    messages.append({"role": "user", "content": ctx.message_text})

    # Force the tool's Langfuse prompt as the system message. An empty prompt
    # (unconfigured Langfuse, blank prompt_name, or a blank managed prompt)
    # degrades to EXACT general_assistant behaviour: no system message.
    name = (ctx.tool.prompt_name if ctx.tool else "") or ""
    sys = ctx.langfuse.get_prompt(name, fallback="") if (ctx.langfuse and name) else ""
    if sys.strip():
        messages.insert(0, {"role": "system", "content": sys})

    content = ctx.llm.complete(messages)
    # Record the resolving slug when known so metadata reflects the real tool.
    used = (ctx.tool.slug if ctx.tool and ctx.tool.slug else "prompt_tool")
    return ToolResult(content=content, tool_used=used)
