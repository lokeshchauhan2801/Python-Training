"""Agent runtime services package.

Holds the LangGraph runtime (``graph_service.LangGraphChatService``) and its
``state.GraphState`` TypedDict. Importing this package is side-effect free and
import-safe: ``graph_service`` imports ``langgraph`` LAZILY (inside the graph
build), so merely importing ``agents.services`` never pulls the LangGraph SDK.
"""
