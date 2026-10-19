"""Tool registry package: get_handler(slug) / available_slugs()."""

from __future__ import annotations

from .base import ToolContext, ToolResult, _REGISTRY, register

# Import the handler modules for their @register side effect (each registers
# exactly one slug). Import-safe: handlers import LLM/RAG seams lazily.
from . import general_assistant  # noqa: E402,F401
from . import document_search  # noqa: E402,F401
from . import chat_summarise  # noqa: E402,F401
from . import web_search  # noqa: E402,F401
from . import prompt_tool  # noqa: E402,F401


def get_handler(slug: str):
    """Return the handler registered under ``slug``, or ``None`` if unknown."""
    return _REGISTRY.get(slug)


def available_slugs() -> set[str]:
    """Return the set of slugs with a registered handler."""
    return set(_REGISTRY.keys())


__all__ = [
    "ToolContext",
    "ToolResult",
    "register",
    "get_handler",
    "available_slugs",
]
