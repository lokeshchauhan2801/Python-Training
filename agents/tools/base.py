"""Tool registry: a ``slug -> handler(ctx) -> ToolResult`` dict."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolContext:
    # Everything a handler needs, passed in by the runtime so handlers never
    # construct these seams themselves. `tool` is the agents.models.Tool row,
    # `llm` is a basics.llm.LLMClient, `langfuse` is the import-safe wrapper.
    user: object
    company: object
    conversation: object
    message_text: str
    history_text: str = ""
    attachment_ids: list = field(default_factory=list)
    model_name: str | None = None
    tool: object | None = None
    llm: object | None = None
    langfuse: object | None = None


@dataclass
class ToolResult:
    # `tool_used` is the slug stamped onto Message.metadata by the runtime.
    # `ok=False` + `error` is a graceful user-visible failure (e.g. the
    # web_search stub), not an exception — the turn still produces a message.
    content: str
    citations: list = field(default_factory=list)
    chart_data: dict | None = None
    tool_used: str = ""
    ok: bool = True
    error: str | None = None


# Module-level registry populated by @register at handler-module import time.
_REGISTRY: dict[str, Callable[[ToolContext], ToolResult]] = {}


def register(slug: str):
    """Decorator registering ``handler`` under ``slug`` in the module registry."""

    def _decorator(handler: Callable[[ToolContext], ToolResult]):
        _REGISTRY[slug] = handler
        return handler

    return _decorator
