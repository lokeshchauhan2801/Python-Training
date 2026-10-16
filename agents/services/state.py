"""The LangGraph runtime's shared state.

``GraphState`` is the ``TypedDict`` threaded through the compiled ``StateGraph``.
It is intentionally plain data (no DB handles, no ORM rows) so the graph is
testable and the LangGraph checkpointer can serialise it. ``ToolResult`` is
re-exported from ``agents.tools.base`` so callers of the runtime have one import
site for the result shape without reaching into the registry package.
"""

from __future__ import annotations

from typing import TypedDict

from agents.tools.base import ToolResult

__all__ = ["GraphState", "ToolResult"]


class GraphState(TypedDict, total=False):
    """State threaded through the compiled graph.

    * ``message_text`` — the current user turn.
    * ``history_text`` — the condensed conversation context for prompts.
    * ``enabled_tools`` — slugs the router node may pick (already intersected
      with globally-enabled + registry-resolvable tools).
    * ``selected_tool`` — the slug the router chose (or ``None`` before routing).
    * ``route_reason`` — a short human reason the router recorded.
    * ``result`` — the executed tool's ``ToolResult`` (or ``None``).
    * ``visited`` — node slugs already entered, for the cycle guard.
    * ``hops`` — total node entries, for the ``MAX_HOPS`` guard.
    """

    message_text: str
    history_text: str
    enabled_tools: list[str]
    selected_tool: str | None
    route_reason: str
    result: ToolResult | None
    visited: list[str]
    hops: int
