"""GraphState: the plain TypedDict threaded through the compiled StateGraph."""

from __future__ import annotations

from typing import TypedDict

from agents.tools.base import ToolResult

__all__ = ["GraphState", "ToolResult"]


class GraphState(TypedDict, total=False):
    message_text: str  # current user turn
    history_text: str  # condensed conversation context for prompts
    enabled_tools: list[str]  # slugs the router may pick (already filtered)
    selected_tool: str | None  # slug the router chose (None before routing)
    route_reason: str  # short human reason the router recorded
    result: ToolResult | None  # the executed tool's result
    visited: list[str]  # node slugs entered, for the cycle guard
    hops: int  # total node entries, for the MAX_HOPS guard
