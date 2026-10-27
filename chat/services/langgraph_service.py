from __future__ import annotations
"""LangGraph chat runtime.

This file holds ONLY the graph implementation: the router prompt, the
``LangGraphChatService`` that compiles a company's agent graph into a
``langgraph`` ``StateGraph``, routes each message to one tool, and persists the
resulting conversation turn.

The tools themselves (and the registry) live in ``chat/services/llm_tools.py``.
They are imported here and the registry symbols are re-exported so existing
imports of ``chat.services.langgraph_service.available_slugs`` /
``get_handler`` keep working unchanged.
"""

import logging
from typing import TypedDict

from django.db import transaction
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

# Tool plumbing + handlers live in llm_tools; importing the module also
# registers every @register handler. Re-exported below for back-compat.
from chat.services.llm_tools import (  # noqa: F401
    ToolContext,
    ToolResult,
    available_slugs,
    get_handler,
    register,
)

logger = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    message_text: str
    history_text: str
    enabled_tools: list[str]
    selected_tool: str | None
    route_reason: str
    result: ToolResult | None
    visited: list[str]
    hops: int


DIRECT = "__direct__"
_DIRECT_HANDLER_SLUG = "general_assistant"

_FALLBACK_ROUTER_PROMPT = (
    "You are a router that selects the single best tool to answer the user's "
    "message, or answers directly when no tool fits.\n"
    "Available tools (slug: description):\n{tools}\n\n"
    "Routing rules:\n"
    "- This is a private workspace where the user uploads their own documents "
    "(letters, profiles, policies, employee records, etc.). If 'document_search' "
    "is available, PREFER it for ANY question that could plausibly be answered "
    "from those uploaded files — including questions about specific named people, "
    "employees, companies, dates, figures, or 'do you know <someone/something>' "
    "questions. When unsure whether the answer is in the documents, choose "
    "'document_search' rather than answering from general knowledge.\n"
    "- Choose 'general_assistant' (or '__direct__') ONLY for questions clearly "
    "about general world knowledge unrelated to the user's files (e.g. 'who is "
    "the prime minister of India', 'explain recursion').\n"
    "- Choose 'chat_summarise' whenever the user asks to summarise, recap, or "
    "review this conversation (e.g. 'chat summary', 'summarise this chat', "
    "'give me a summary'). This takes priority over document_search for explicit "
    "summary requests.\n"
    "- Choose 'web_search' for current/external web information, AND for chart "
    "requests whose data comes from general world knowledge rather than the "
    "user's files (e.g. 'pie chart of India's GDP growth last 5 years', 'bar "
    "chart of world populations') — but ONLY when NO file is attached on this "
    "message. It can build the chart from its own knowledge.\n"
    "- Choose 'document_search' for chart/graph requests whose data comes from "
    "an attached or uploaded file (e.g. 'make a bar chart from my CSV', 'chart "
    "the figures in this document'). IMPORTANT: when a file IS attached on this "
    "message (see the attachment note below), a bare chart request such as "
    "'create a pie chart' means chart THAT attached file — choose "
    "'document_search', never 'web_search'/'general_assistant', and never reuse "
    "data from an earlier turn.\n"
    "- Choose 'add_to_knowledge_base' when the user asks to ADD, SAVE, STORE, "
    "UPLOAD, INDEX, or PROCESS an attached file into the knowledge base / "
    "document library / company documents (e.g. 'add this file to the knowledge "
    "base', 'process this document'). This triggers background Temporal "
    "processing (chunk + embed); do NOT use it for merely asking questions about "
    "a file.\n"
    "- Choose 'document_processing_status' when the user asks about the STATUS, "
    "PROGRESS, or count of documents being processed, or the status of a "
    "specific document/task id (e.g. 'what's processing right now', 'is my file "
    "done', 'status of doc-01H...').\n\n"
    "Conversation so far:\n{history}\n\n"
    "{attachment_note}\n"
    "User message:\n{question}\n\n"
    "Choose exactly one tool slug from the list above, or '__direct__' to answer "
    "directly without a tool. Respond with the chosen slug and a short reason."
)


class RouterDecision(PydanticBaseModel):
    """The structured output of the LLM router."""

    tool: str
    reason: str = Field(default="", max_length=300)


class LangGraphChatService:

    def __init__(self, chat_service, langfuse):
        self._chat = chat_service
        self._langfuse = langfuse


    def process_message(
        self,
        user,
        message_text,
        conversation=None,
        company=None,
        attachment_ids=None,
        model_name=None,
    ):
        """Route one message through the graph and persist one turn.

        Returns the created ``Conversation`` turn (same as ``ChatService``).
        Raises only the exception types ``chat.views.send`` already catches
        (``LLMConfigurationError``, ``LLMBackendError``, ``ChatProviderError``,
        ``ChatRetrievalError``) so the view's HTTP status mapping is unchanged.
        """
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.retitle import retitle_if_default
        from chat.services import ChatProviderError, ChatRetrievalError

        if not user or not user.is_authenticated:
            raise ValueError("A signed-in user is required to chat.")
        if conversation and (
            conversation.user_id != user.pk
            or conversation.company_id != getattr(company, "pk", None)
        ):
            raise ValueError("Conversation does not belong to the signed-in user.")

        llm = self._chat._llm


        history_text = self._history_text(conversation)

        step_node, enabled_tools = self._resolve_step_and_tools(company)


        try:
            result = self._run_graph(
                user=user,
                message_text=message_text,
                conversation=conversation,
                company=company,
                attachment_ids=attachment_ids,
                model_name=model_name,
                history_text=history_text,
                step_node=step_node,
                enabled_tools=enabled_tools,
                llm=llm,
            )
        except (
            LLMConfigurationError,
            LLMBackendError,
            ChatProviderError,
            ChatRetrievalError,
        ):
            raise
        except Exception as exc:
            logger.exception("LangGraph turn failed unexpectedly")
            raise ChatProviderError(
                "The LLM provider failed to generate a response."
            ) from exc

        tool_result = result["tool_result"]
        content = (tool_result.content or "").strip()
        if not content:
            raise ChatProviderError("The LLM provider returned an empty response.")


        assistant_model_name = model_name or getattr(llm, "model", None)


        with transaction.atomic():
            convo = conversation
            turn = self._chat.create_turn(
                convo,
                user=user,
                question=message_text,
                response=content,
                model_name=assistant_model_name,
                citations=tool_result.citations or [],
                chart_data=tool_result.chart_data,
                metadata={
                    "tool_used": tool_result.tool_used or "",
                    "node_slug": result["node_slug"],
                    "route_reason": result["route_reason"],
                    "source": "langgraph",
                    "trace_id": result["trace_id"],
                },
                attachment_ids=attachment_ids,
                actor=user,
            )

            retitle_if_default(
                convo,
                llm_title=None,
                fallback_text=message_text,
                actor=user,
            )
        return turn


    def _run_graph(
        self,
        *,
        user,
        message_text,
        conversation,
        company,
        attachment_ids,
        model_name,
        history_text,
        step_node,
        enabled_tools,
        llm,
    ):
        from agents.models import AgentEdge, AgentNode

        node_slug = step_node.slug


        outcome = {
            "tool_result": None,
            "tool_used": "",
            "node_slug": node_slug,
            "route_reason": "",
            "trace_id": None,
        }

        def _ctx(tool_row):
            return ToolContext(
                user=user,
                company=company,
                conversation=conversation,
                message_text=message_text,
                history_text=history_text,
                attachment_ids=list(attachment_ids or []),
                model_name=model_name,
                tool=tool_row,
                llm=llm,
                langfuse=self._langfuse,
            )

        tool_rows = {t.slug: t for t in step_node.tools.all()}

        def _execute(slug):
            """Run the handler for ``slug`` (or the direct handler) -> ToolResult."""
            handler_slug = _DIRECT_HANDLER_SLUG if slug == DIRECT else slug
            handler = get_handler(handler_slug)
            tool_row = tool_rows.get(handler_slug)
            if handler is None:


                prompt_only = (
                    tool_row is not None
                    and isinstance(tool_row.config, dict)
                    and tool_row.config.get("kind") == "prompt_only"
                )
                handler = get_handler(
                    "prompt_tool" if prompt_only else _DIRECT_HANDLER_SLUG
                )
            result = handler(_ctx(tool_row))


            if not result.tool_used:
                result.tool_used = handler_slug
            return result

        has_attachment = bool(attachment_ids)

        def _router_body(state):
            from chat.services.chat_service import ChatService

            # Deterministic rule: a chart request WITH a file attached this turn
            # must chart THAT file (never world-knowledge / a previous turn's
            # data). Force document_search, bypassing the LLM router, so e.g.
            # "create a pie chart" + attached CSV never reuses an earlier chart.
            if (
                has_attachment
                and ChatService._is_chart_request(message_text)
                and "document_search" in enabled_tools
            ):
                selected = "document_search"
                reason = (
                    "file attached this turn + chart request -> chart the "
                    "attached file"
                )
                trace_id = None
            else:
                selected, reason, trace_id = self._route(
                    message_text=message_text,
                    history_text=history_text,
                    enabled_tools=enabled_tools,
                    step_node=step_node,
                    tool_rows=tool_rows,
                    llm=llm,
                    has_attachment=has_attachment,
                )
            result = _execute(selected)
            outcome["tool_result"] = result
            outcome["tool_used"] = result.tool_used
            outcome["route_reason"] = reason
            outcome["trace_id"] = trace_id
            return {
                "selected_tool": selected,
                "route_reason": reason,
                "visited": (state.get("visited") or []) + [state["_current"]],
                "hops": state.get("hops", 0) + 1,
            }


        graph = step_node.graph
        nodes = list(
            AgentNode.objects.filter(graph=graph).order_by("slug")
        )
        edges = list(
            AgentEdge.objects.filter(graph=graph)
            .select_related("source", "target")
            .order_by("source__slug", "target__slug")
        )

        app = self._build_graph(
            nodes=nodes,
            edges=edges,
            step_slug=node_slug,
            router_body=_router_body,
        )

        app.invoke(
            {
                "message_text": message_text,
                "history_text": history_text,
                "enabled_tools": list(enabled_tools),
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )


        if outcome["tool_result"] is None:
            result = _execute(DIRECT)
            outcome["tool_result"] = result
            outcome["tool_used"] = result.tool_used
            outcome["route_reason"] = "no router node executed; answered directly"
        return outcome

    def _build_graph(self, *, nodes, edges, step_slug, router_body):
        """Compile a generic ``StateGraph`` from node/edge rows.

        ``langgraph`` is imported HERE (lazily) so importing this module stays
        SDK-free. Edges are followed in deterministic ``(source_slug,
        target_slug)`` order; the per-node router routes to the first not-yet-
        visited target, else END. The ``visited`` set + ``MAX_HOPS`` hop cap
        guarantee termination even if the admin wired a cycle.
        """
        from langgraph.graph import END, START, StateGraph


        kind_by_slug = {n.slug: n.kind for n in nodes}
        start_slug = next((n.slug for n in nodes if n.kind == "start"), None)

        def _resolve(slug):


            kind = kind_by_slug.get(slug)
            if kind == "start":
                return START
            if kind == "end":
                return END
            return slug

        step_slugs = [n.slug for n in nodes if kind_by_slug.get(n.slug) == "step"]
        node_count = max(len(step_slugs), 1)
        max_hops = 2 * node_count + 1


        adjacency: dict[object, list[object]] = {}
        for edge in edges:
            src = _resolve(edge.source.slug)
            tgt = _resolve(edge.target.slug)
            adjacency.setdefault(src, []).append(tgt)

        builder = StateGraph(GraphState)

        def _make_node(slug):
            is_router = slug == step_slug

            def _node(state):
                state = dict(state)
                state["_current"] = slug
                if is_router:
                    return router_body(state)
                return {
                    "visited": (state.get("visited") or []) + [slug],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node


        for slug in step_slugs:
            builder.add_node(slug, _make_node(slug))


        start_targets = adjacency.get(START, [])
        entry = next((t for t in start_targets if t in step_slugs), None)
        if entry is None:
            entry = step_slugs[0] if step_slugs else END
        builder.add_edge(START, entry)

        def _make_choice(slug):
            targets = adjacency.get(slug, [])

            def _choose(state):
                if state.get("hops", 0) > max_hops:
                    return END
                visited = state.get("visited") or []
                for tgt in targets:

                    if tgt == END or tgt not in visited:
                        return tgt
                return END

            return _choose


        for slug in step_slugs:
            targets = adjacency.get(slug, [])
            mapping = {t: t for t in targets if t in step_slugs}
            mapping[END] = END
            builder.add_conditional_edges(slug, _make_choice(slug), mapping)

        return builder.compile()


    def _route(
        self,
        *,
        message_text,
        history_text,
        enabled_tools,
        step_node,
        tool_rows,
        llm,
        has_attachment=False,
    ):
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.services import ChatProviderError

        if not enabled_tools:
            return (
                DIRECT,
                "no tools enabled on the node; answered directly",
                None,
            )

        tools_desc = "\n".join(
            f"{slug}: {self._tool_description(tool_rows.get(slug), slug)}"
            for slug in enabled_tools
        )
        attachment_note = (
            "Attachment note: the user attached a file on THIS message, so a "
            "chart request means chart that file and any file question is about "
            "it — strongly prefer 'document_search'."
            if has_attachment
            else "Attachment note: no file is attached on this message."
        )
        prompt_name = step_node.prompt_name or "agent_router"
        template = self._langfuse.get_prompt(
            prompt_name, fallback=_FALLBACK_ROUTER_PROMPT
        )
        fmt = {
            "question": message_text,
            "tools": tools_desc,
            "history": history_text or "(no prior conversation)",
            "attachment_note": attachment_note,
        }
        try:
            router_prompt = template.format(**fmt)
        except (KeyError, IndexError):
            # A managed prompt may omit {attachment_note}; fall back to the
            # built-in template, appending the note so routing stays correct.
            try:
                router_prompt = _FALLBACK_ROUTER_PROMPT.format(**fmt)
            except (KeyError, IndexError):
                router_prompt = template.format(
                    question=message_text,
                    tools=tools_desc,
                    history=history_text or "(no prior conversation)",
                )
                router_prompt = f"{router_prompt}\n\n{attachment_note}"

        trace_id = None
        with self._langfuse.trace("agent_router") as span:
            trace_id = self._trace_id(span)
            try:
                decision = llm.generate_structured(router_prompt, RouterDecision)
            except LLMConfigurationError:
                raise
            except LLMBackendError as exc:
                raise ChatProviderError(
                    "The LLM provider failed to route the message."
                ) from exc

        allowed = set(enabled_tools) | {DIRECT}
        selected = (decision.tool or "").strip()
        reason = (decision.reason or "").strip()
        if selected not in allowed:
            logger.info(
                "router returned unknown tool '%s'; answering directly", selected
            )
            return (
                DIRECT,
                f"router returned unknown tool '{selected}', answered directly",
                trace_id,
            )
        return (selected, reason, trace_id)


    def _resolve_step_and_tools(self, company):
        """Return ``(step_node, enabled_tools)`` for the company's active graph.

        ``_orchestrator_for`` only routes here when a runnable graph exists, but
        this re-resolves defensively. ``enabled_tools`` is the intersection of
        the STEP node's attached tools, globally ``is_enabled`` tools, and
        registry-resolvable slugs. May be empty (router forces ``__direct__``).
        """
        from agents.models import AgentGraph, AgentNode

        graph = (
            AgentGraph.objects.filter(company=company, is_active=True)
            .order_by("-created_at")
            .first()
        )
        step_node = (
            AgentNode.objects.filter(graph=graph, kind=AgentNode.KIND_STEP)
            .order_by("created_at")
            .first()
        )
        slugs = available_slugs()
        enabled = []
        seen = set()
        for tool in step_node.tools.filter(is_enabled=True):


            prompt_only = (
                isinstance(tool.config, dict)
                and tool.config.get("kind") == "prompt_only"
            )
            if (tool.slug in slugs or prompt_only) and tool.slug not in seen:
                enabled.append(tool.slug)
                seen.add(tool.slug)
        return step_node, enabled

    def _history_text(self, conversation):
        """Build the condensed conversation context, mirroring the RAG path."""
        if conversation is None:
            return ""
        turns = list(conversation.turns.order_by("created_at"))
        return self._chat._build_conversation_context(turns)

    @staticmethod
    def _tool_description(tool_row, slug):
        if tool_row is not None and (tool_row.description or "").strip():
            return tool_row.description.strip()
        if tool_row is not None and (tool_row.name or "").strip():
            return tool_row.name.strip()
        return slug

    @staticmethod
    def _trace_id(span):
        """Best-effort read of a Langfuse trace id from a span object."""
        if span is None:
            return None
        for attr in ("id", "trace_id"):
            value = getattr(span, attr, None)
            if isinstance(value, str) and value:
                return value
        return None
