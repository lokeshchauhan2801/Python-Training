"""``web_search`` tool — STUB.

The seam for a real web-search provider, deliberately not wired to any paid
dependency, API key, or network call in this phase. It returns a graceful
``ToolResult(ok=False, ...)`` rather than raising, so a turn routed here still
produces a user-visible assistant message (with the ``web_search`` badge)
instead of a 502. The handler body is the single place a real provider is wired
later.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult, register


@register("web_search")
def handle(ctx: ToolContext) -> ToolResult:
    return ToolResult(
        content="Web search is not configured on this server.",
        ok=False,
        error="web_search_not_configured",
        tool_used="web_search",
    )
