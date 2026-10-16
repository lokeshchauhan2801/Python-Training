"""``document_search`` tool — the RAG/document path reused as a tool.

Reuses ``ChatService`` internals so behaviour is identical to the existing RAG
pipeline: ``_find_relevant_chunks`` (which owns the empty-collection guard and
the ``ChatRetrievalError`` wrapping — never the lower-level
``documents.services.find_relevant_chunks`` directly), then ``_build_prompt`` +
``generate_structured(prompt, ChatCompletion)``. Returns the cited, optionally
charted answer.

All chat/LLM imports are lazy to keep ``agents.tools`` import-safe.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("document_search")
def handle(ctx: ToolContext) -> ToolResult:
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
