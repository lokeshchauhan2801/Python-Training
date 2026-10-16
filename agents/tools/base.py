"""Tool registry seam.

A tool is a pure function ``handler(ctx: ToolContext) -> ToolResult``. The
registry is an in-process ``slug -> handler`` dict, populated by the
``@register("slug")`` decorator when each handler module is imported (see
``agents/tools/__init__.py``). Importing this module (or the package) has NO
network or key side effects — the handlers import the LLM/RAG seams lazily,
inside the function body, only when actually called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ToolContext:
    """Everything a tool handler needs to produce an answer.

    ``tool`` is the ``agents.models.Tool`` row (carries per-tool ``config``);
    ``llm`` is a ``basics.llm.LLMClient`` instance; ``langfuse`` is the
    import-safe wrapper (may be a no-op). All are passed in by the runtime so a
    handler never constructs these seams itself (keeps handlers testable and
    import-safe).
    """

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
    """The outcome of running a tool handler.

    ``tool_used`` is the slug stamped onto ``Message.metadata`` by the runtime.
    ``ok=False`` + ``error`` is a graceful, user-visible failure (e.g. the
    web_search stub), NOT an exception — the chat turn still produces a message.
    """

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
