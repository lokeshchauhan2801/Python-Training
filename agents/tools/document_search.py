"""``document_search`` tool — the RAG/document path reused as a tool."""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("document_search")
def handle(ctx: ToolContext) -> ToolResult:
    # Reuse ChatService internals so behaviour matches the RAG pipeline exactly
    # (_find_relevant_chunks owns the empty-collection guard + retrieval-error
    # wrapping). Lazy import keeps agents.tools import-safe.
    from chat.services import ChatCompletion, ChatService

    service = ChatService()
    chunks = service._find_relevant_chunks(
        ctx.user,
        ctx.message_text,
        conversation=ctx.conversation,
        company=ctx.company,
    )
    prompt, citations = service._build_prompt(
        ctx.history_text,
        ctx.message_text,
        chunks,
    )
    # Optional Langfuse preamble prepended to the RAG prompt; empty fallback
    # leaves the structured prompt untouched (today's behaviour).
    name = (ctx.tool.prompt_name if ctx.tool else "") or "tool_document_search"
    pre = ctx.langfuse.get_prompt(name, fallback="") if ctx.langfuse else ""
    if pre.strip():
        prompt = f"{pre.strip()}\n\n{prompt}"
    completion = ctx.llm.generate_structured(prompt, ChatCompletion)
    chart_data = (
        completion.chart.model_dump(mode="json") if completion.chart else None
    )
    return ToolResult(
        content=completion.response.strip(),
        citations=citations,
        chart_data=chart_data,
        tool_used="document_search",
    )
