"""Moved to chat/services/graph_state.py — thin back-compat shim."""

from chat.services.graph_state import GraphState, ToolResult  # noqa: F401

__all__ = ["GraphState", "ToolResult"]
