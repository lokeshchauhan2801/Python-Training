"""``web_search`` tool — STUB seam for a real provider (wire it in handle)."""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("web_search")
def handle(ctx: ToolContext) -> ToolResult:
    # Graceful failure (ok=False), not an exception, so the turn still produces
    # a message with the web_search badge. Wire a real provider here later.
    return ToolResult(
        content="Web search is not configured on this server.",
        ok=False,
        error="web_search_not_configured",
        tool_used="web_search",
    )
