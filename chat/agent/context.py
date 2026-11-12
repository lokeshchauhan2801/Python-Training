"""The per-run context every node and tool of the Agent graph receives.

Passed as ``graph.stream(..., context=AgentContext(...))`` (LangGraph's
``context_schema``); tools read it through ``ToolRuntime.context``. It is not
part of the checkpointed state.
"""
from dataclasses import dataclass, field


@dataclass
class AgentContext:
    user: object
    company: object
    session: object  # the ConversationSession (the thread)
    agent: object
    llm: object  # basics.llm.LLMClient built from the agent's config
    role: str = ""
    message_text: str = ""
    attachment_ids: list = field(default_factory=list)
    # Picker selection (validated SessionFile entry ids) for a process request.
    selected_entry_ids: list = field(default_factory=list)
    retrieval_top_k: int | None = None
    # Tool rows the model may call, keyed by slug.
    tools: dict = field(default_factory=dict)
    timing: object | None = None
