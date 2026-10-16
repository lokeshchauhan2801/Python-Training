"""Tool registry package.

Importing this package imports the four built-in handler modules so their
``@register`` decorators run and populate ``base._REGISTRY``. This has NO
network/key side effects: the handler modules only define functions and import
the LLM/RAG seams lazily inside those functions.

Public surface:
  * ``get_handler(slug)`` -> handler callable or ``None``.
  * ``available_slugs()`` -> set of registered slugs.
"""

from __future__ import annotations

from .base import ToolContext, ToolResult, _REGISTRY, register

# Import the handler modules for their @register side effect. Order is
# irrelevant; each module registers exactly one slug.
from . import general_assistant  # noqa: E402,F401
from . import document_search  # noqa: E402,F401
from . import chat_summarise  # noqa: E402,F401
from . import web_search  # noqa: E402,F401


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
