"""Load an Agent's graph JSON (``Agent.graph``) into a runnable LangGraph graph.

Chat never reads Steps: the graph comes only from the JSON stored on the
Agent (shape in ``agents/services/graph_json.py``). ``build_graph`` maps it
one-to-one onto a ``StateGraph``:

* ``model`` node  -> calls the agent's model with the node's ``tools`` bound
* ``tools`` node  -> a ``ToolNode`` running the tools of the model node it
  is ``for``
* edge without a ``condition`` -> ``add_edge``
* edges with a ``condition`` from one node -> ``add_conditional_edges`` with
  a ``{"tools": ..., "next": ...}`` path map ("tools" when the reply asks for
  tools, "next" otherwise)

Compiled graphs are cached by their JSON, so a graph compiles once per
process until the Agent's JSON changes.
"""
from __future__ import annotations

import json
import threading

from django.conf import settings
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from chat.services.agent.context import AgentContext
from chat.services.agent.prompts import attached_files_text, system_prompt
from chat.services.agent.tools import tools_for

_CACHE_SIZE = 64
_lock = threading.Lock()
_compiled: dict[str, object] = {}
_checkpointer = None


def _recent(messages, turns: int):
    """The messages of the last ``turns`` user turns (tool calls stay paired)."""
    if turns <= 0:
        return list(messages)
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(starts) <= turns:
        return list(messages)
    return list(messages[starts[-turns]:])


# --------------------------------------------------------------------------- #
# Reading the graph JSON
# --------------------------------------------------------------------------- #
def model_nodes(graph: dict) -> list[dict]:
    """The graph's ``model`` nodes, in stored (run) order."""
    return [n for n in (graph or {}).get("nodes", []) if n.get("type") == "model"]


def answer_node(graph: dict) -> str | None:
    """The model node whose reply is the answer (the one that reaches END)."""
    models = {n["id"] for n in model_nodes(graph)}
    for edge in (graph or {}).get("edges", []):
        if edge["target"] == END and edge["source"] in models:
            return edge["source"]
    return None


def tool_slugs(graph: dict) -> set[str]:
    """Every tool slug a node of the graph binds."""
    return {slug for n in model_nodes(graph) for slug in n.get("tools", [])}


def node_step(graph: dict) -> dict[str, str]:
    """``{node id: model node id}`` (a tools node belongs to its model node)."""
    out = {}
    for n in (graph or {}).get("nodes", []):
        if n.get("type") == "model":
            out[n["id"]] = n["id"]
        elif n.get("type") == "tools":
            out[n["id"]] = n.get("for", "")
    return out


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def _with_files(messages, files_text: str):
    """``messages`` with this turn's file content added to the user's latest
    message, for this model call only (never saved in the thread).

    In the user's own message the model trusts it over earlier replies that
    said the file could not be read; the system prompt alone was ignored.
    """
    if not files_text:
        return messages
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last = messages[i]
            text = last.content if isinstance(last.content, str) else str(last.content)
            out = list(messages)
            out[i] = HumanMessage(f"{text}\n\n{files_text}")
            return out
    return messages


def _model_node(node: dict, index: int, total: int):
    slugs = list(node.get("tools", []))
    name = node.get("name") or node["id"]

    def call_model(state: MessagesState, config, runtime):
        ctx = runtime.context
        tools = tools_for(ctx, slugs)
        model = ctx.llm._ensure_client()
        if tools:
            model = model.bind_tools(tools)
        prompt = system_prompt(
            ctx.role,
            ctx.llm,
            [t.name for t in tools],
            ctx.files_note,
        )
        if total > 1:
            prompt += f'\n\nYou are running step {index + 1} of {total} ("{name}") of this agent\'s flow.'
            if index > 0:
                prompt += " Earlier steps already replied above; build on their output and do not repeat it."
        messages = [SystemMessage(prompt)]
        messages += _with_files(
            _recent(state["messages"], int(getattr(settings, "CHAT_RECENT_TURNS", 12))),
            attached_files_text(ctx.attached_files),
        )
        # ``config`` is passed on so stream_mode="messages" sees the tokens
        # (Python 3.10 has no async context propagation).
        return {"messages": [model.invoke(messages, config)]}

    return call_model


def _tool_node(slugs: list[str]):
    from langgraph.prebuilt import ToolNode

    slugs = list(slugs)

    def run_tools(state: MessagesState, config, runtime):
        # handle_tool_errors=True: a bad call returns the real error (e.g. "Each
        # chart point must have one value per series.") so the model fixes it.
        return ToolNode(
            tools_for(runtime.context, slugs), handle_tool_errors=True
        ).invoke(state, config)

    return run_tools


def _route(state: MessagesState) -> str:
    last = state["messages"][-1] if state.get("messages") else None
    return "tools" if getattr(last, "tool_calls", None) else "next"


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #
def build_graph(graph: dict, checkpointer=None):
    """Compile graph JSON. Raises ``ValueError`` for an empty or bad graph."""
    nodes = (graph or {}).get("nodes") or []
    edges = (graph or {}).get("edges") or []
    models = model_nodes(graph)
    if not models:
        raise ValueError("The selected agent does not have a runnable flow.")

    builder = StateGraph(MessagesState, context_schema=AgentContext)
    for i, node in enumerate(models):
        builder.add_node(node["id"], _model_node(node, i, len(models)))
    tools_of = {n["id"]: n.get("tools", []) for n in models}
    for node in nodes:
        if node.get("type") == "tools":
            if node.get("for") not in tools_of:
                raise ValueError(f"Tools node {node['id']!r} is not for a model node.")
            builder.add_node(node["id"], _tool_node(tools_of[node["for"]]))
        elif node.get("type") not in ("model", "start", "end"):
            raise ValueError(f"Unknown graph node type {node.get('type')!r}.")

    conditional: dict[str, dict[str, str]] = {}
    for edge in edges:
        if edge.get("condition"):
            conditional.setdefault(edge["source"], {})[edge["condition"]] = edge["target"]
        else:
            builder.add_edge(edge["source"], edge["target"])
    for source, path_map in conditional.items():
        builder.add_conditional_edges(source, _route, path_map)
    return builder.compile(checkpointer=checkpointer, name=graph.get("agent") or "agent")


def get_checkpointer():
    global _checkpointer
    if _checkpointer is None:
        from chat.services.agent.checkpointer import get_checkpointer as make

        _checkpointer = make()
    return _checkpointer


def get_graph(graph: dict):
    """The compiled graph for this JSON with the Postgres checkpointer (cached)."""
    key = json.dumps(graph, sort_keys=True)
    compiled = _compiled.get(key)
    if compiled is None:
        with _lock:
            compiled = _compiled.get(key)
            if compiled is None:
                if len(_compiled) >= _CACHE_SIZE:
                    _compiled.clear()
                compiled = build_graph(graph, get_checkpointer())
                _compiled[key] = compiled
    return compiled


__all__ = [
    "START",
    "END",
    "answer_node",
    "build_graph",
    "get_checkpointer",
    "get_graph",
    "model_nodes",
    "node_step",
    "tool_slugs",
]
