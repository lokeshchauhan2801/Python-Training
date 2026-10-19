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

    # Resolve the system prompt from Langfuse, falling back to the hard-coded
    # prompt when the tool/langfuse seam is absent or unconfigured.
    name = (ctx.tool.prompt_name if ctx.tool else "") or "tool_chat_summarise"
    sys = (
        ctx.langfuse.get_prompt(name, fallback=_SUMMARY_SYSTEM_PROMPT)
        if ctx.langfuse
        else _SUMMARY_SYSTEM_PROMPT
    )

    try:
        # A managed prompt may template the context in itself; if so, inline it
        # and send a single system message. Otherwise keep today's shape.
        if "{{conversation}}" in sys or "{{history}}" in sys:
            system_content = sys.replace(
                "{{conversation}}", conversation_context
            ).replace("{{history}}", conversation_context)
            messages = [{"role": "system", "content": system_content}]
        else:
            messages = [
                {"role": "system", "content": sys},
                {"role": "user", "content": conversation_context},
            ]
    except Exception:  # noqa: BLE001 - any substitution issue degrades to fallback
        messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": conversation_context},
        ]

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="chat_summarise")
