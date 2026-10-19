"""LangGraphChatService — runs one new message through a company's graph.

A turn: resolve the active graph's STEP (router) node and its enabled tools ->
one structured LLM call picks a tool slug (or __direct__) -> run the handler ->
persist. The public entry point mirrors ChatService.process_message so
chat.views.send can call either interchangeably. langgraph is imported lazily
inside _build_graph so importing this module never pulls the SDK.
"""

from __future__ import annotations

import logging

from django.db import transaction
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field

logger = logging.getLogger(__name__)

# The sentinel the router returns to answer directly (no specialised tool). It
# runs the ``general_assistant`` handler.
DIRECT = "__direct__"
_DIRECT_HANDLER_SLUG = "general_assistant"

# Local fallback router prompt used when Langfuse has no managed prompt (or is
# not configured). ``{question}``/``{tools}``/``{history}`` are filled in.
_FALLBACK_ROUTER_PROMPT = (
    "You are a router that selects the single best tool to answer the user's "
    "message, or answers directly when no tool fits.\n"
    "Available tools (slug: description):\n{tools}\n\n"
    "Conversation so far:\n{history}\n\n"
    "User message:\n{question}\n\n"
    "Choose exactly one tool slug from the list above, or '__direct__' to answer "
    "directly without a tool. Respond with the chosen slug and a short reason."
)


class RouterDecision(PydanticBaseModel):
    """The structured output of the LLM router."""

    tool: str
    reason: str = Field(default="", max_length=300)


class LangGraphChatService:
    """Run a new chat message through the company's active LangGraph graph.

    Constructed with an injected ``ChatService`` (reused for persistence + RAG
    internals) and an import-safe Langfuse client (may be a no-op). The LLM seam
    is taken from the injected ``chat_service`` so tests inject one fake client
    and both the router and the tool handlers share it.
    """

    def __init__(self, chat_service, langfuse):
        self._chat = chat_service
        self._langfuse = langfuse

    # ------------------------------------------------------------------ #
    # Public entry point — mirrors ChatService.process_message's shape.  #
    # ------------------------------------------------------------------ #
    def process_message(
        self,
        user,
        message_text,
        conversation=None,
        company=None,
        attachment_ids=None,
        model_name=None,
    ):
        """Route one message through the graph and persist the Message pair.

        Returns the assistant ``Message`` (same as ``ChatService``). Raises only
        the exception types ``chat.views.send`` already catches
        (``LLMConfigurationError``, ``LLMBackendError``, ``ChatProviderError``,
        ``ChatRetrievalError``) so the view's HTTP status mapping is unchanged.
        """
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.models import Message
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

        # Condensed history for prompts — assembled exactly like the RAG path
        # (non-system messages, oldest first), BEFORE the current turn is saved.
        history_text = self._history_text(conversation)

        step_node, enabled_tools = self._resolve_step_and_tools(company)

        # Build + run the graph. Known/mapped failures surface as the existing
        # exception types; anything unexpected becomes a ChatProviderError (502).
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
        except Exception as exc:  # noqa: BLE001 - unexpected tool/graph failure
            logger.exception("LangGraph turn failed unexpectedly")
            raise ChatProviderError(
                "The LLM provider failed to generate a response."
            ) from exc

        tool_result = result["tool_result"]
        content = (tool_result.content or "").strip()
        if not content:
            raise ChatProviderError("The LLM provider returned an empty response.")

        assistant_model_name = model_name or None

        # Persist the user + assistant pair AND the metadata atomically.
        with transaction.atomic():
            convo = conversation
            user_message = self._chat.create_message(
                convo,
                Message.ROLE_USER,
                message_text,
                model_name="user",
                actor=user,
            )
            self._chat._link_attachments(user_message, convo, attachment_ids)
            assistant_message = self._chat.create_message(
                convo,
                Message.ROLE_ASSISTANT,
                content,
                model_name=assistant_model_name,
                citations=tool_result.citations or [],
                chart_data=tool_result.chart_data,
                actor=user,
            )
            assistant_message.metadata = {
                "tool_used": tool_result.tool_used or "",
                "node_slug": result["node_slug"],
                "route_reason": result["route_reason"],
                "source": "langgraph",
                "trace_id": result["trace_id"],
            }
            assistant_message.save(
                actor=user,
                update_fields=["metadata", "updated_at", "updated_by"],
            )
            # First-message retitle (BUG 2). The graph path has no structured
            # title, so the title comes from message_text[:50] (documented
            # difference from the RAG path).
            retitle_if_default(
                convo,
                llm_title=None,
                fallback_text=message_text,
                actor=user,
            )
        return assistant_message

    # ------------------------------------------------------------------ #
    # Graph construction + execution.                                    #
    # ------------------------------------------------------------------ #
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
        """Build and invoke the StateGraph; return the chosen tool's outcome.

        Returns a dict ``{tool_result, tool_used, node_slug, route_reason,
        trace_id}``. The graph nodes mutate a captured ``outcome`` dict so the
        router's choice/execution survives outside the LangGraph state (which
        only carries JSON-ish scalars).
        """
        from agents.models import AgentEdge, AgentNode
        from agents.tools import get_handler
        from agents.tools.base import ToolContext

        node_slug = step_node.slug

        # Captured outcome, filled by the router node when it runs the handler.
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
                # An unregistered slug that is flagged prompt_only runs through
                # the generic prompt_tool handler (its Langfuse prompt_name
                # drives the answer); otherwise fall back to the direct handler.
                prompt_only = (
                    tool_row is not None
                    and isinstance(tool_row.config, dict)
                    and tool_row.config.get("kind") == "prompt_only"
                )
                handler = get_handler(
                    "prompt_tool" if prompt_only else _DIRECT_HANDLER_SLUG
                )
            result = handler(_ctx(tool_row))
            # Stamp the slug the ROUTER selected (so __direct__ records
            # general_assistant, web_search records web_search, etc.).
            if not result.tool_used:
                result.tool_used = handler_slug
            return result

        def _router_body(state):
            selected, reason, trace_id = self._route(
                message_text=message_text,
                history_text=history_text,
                enabled_tools=enabled_tools,
                step_node=step_node,
                tool_rows=tool_rows,
                llm=llm,
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

        # Build the generic graph from the active graph's node/edge rows.
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

        # If the router node never executed (degenerate graph with no STEP in
        # the traversal), fall back to a direct answer so the turn still yields
        # a message rather than failing.
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

        from .graph_state import GraphState

        node_slugs = [n.slug for n in nodes]
        node_count = max(len(node_slugs), 1)
        max_hops = 2 * node_count + 1

        # Deterministic adjacency: source_slug -> [target_slug, ...] in
        # (source, target) order.
        adjacency: dict[str, list[str]] = {s: [] for s in node_slugs}
        for edge in edges:
            src = edge.source.slug
            tgt = edge.target.slug
            if src in adjacency:
                adjacency[src].append(tgt)

        start_slug = next(
            (n.slug for n in nodes if n.kind == "start"), None
        )

        builder = StateGraph(GraphState)

        def _make_node(slug):
            is_router = slug == step_slug

            def _node(state):
                # Record which concrete node is executing so the router body can
                # append it to ``visited``.
                state = dict(state)
                state["_current"] = slug
                if is_router:
                    return router_body(state)
                return {
                    "visited": (state.get("visited") or []) + [slug],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node

        for slug in node_slugs:
            builder.add_node(slug, _make_node(slug))

        if start_slug and start_slug in node_slugs:
            builder.add_edge(START, start_slug)
        elif node_slugs:
            builder.add_edge(START, node_slugs[0])

        def _make_choice(slug):
            targets = adjacency.get(slug, [])

            def _choose(state):
                if state.get("hops", 0) > max_hops:
                    return END
                visited = state.get("visited") or []
                for tgt in targets:
                    if tgt not in visited:
                        return tgt
                return END

            return _choose

        for slug in node_slugs:
            targets = adjacency.get(slug, [])
            mapping = {t: t for t in targets}
            mapping[END] = END
            builder.add_conditional_edges(slug, _make_choice(slug), mapping)

        return builder.compile()

    # ------------------------------------------------------------------ #
    # The LLM router.                                                    #
    # ------------------------------------------------------------------ #
    def _route(
        self,
        *,
        message_text,
        history_text,
        enabled_tools,
        step_node,
        tool_rows,
        llm,
    ):
        """Pick one enabled tool slug or ``__direct__`` via a structured LLM call.

        Returns ``(selected_slug, route_reason, trace_id)``. With NO enabled
        tools the router is skipped and ``__direct__`` is forced. An unknown slug
        from the model is coerced to ``__direct__``.
        """
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
        prompt_name = step_node.prompt_name or "agent_router"
        template = self._langfuse.get_prompt(
            prompt_name, fallback=_FALLBACK_ROUTER_PROMPT
        )
        try:
            router_prompt = template.format(
                question=message_text,
                tools=tools_desc,
                history=history_text or "(no prior conversation)",
            )
        except (KeyError, IndexError):
            # A managed prompt may use different placeholders; fall back locally.
            router_prompt = _FALLBACK_ROUTER_PROMPT.format(
                question=message_text,
                tools=tools_desc,
                history=history_text or "(no prior conversation)",
            )

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

    # ------------------------------------------------------------------ #
    # Helpers.                                                           #
    # ------------------------------------------------------------------ #
    def _resolve_step_and_tools(self, company):
        """Return ``(step_node, enabled_tools)`` for the company's active graph.

        ``_orchestrator_for`` only routes here when a runnable graph exists, but
        this re-resolves defensively. ``enabled_tools`` is the intersection of
        the STEP node's attached tools, globally ``is_enabled`` tools, and
        registry-resolvable slugs. May be empty (router forces ``__direct__``).
        """
        from agents.models import AgentGraph, AgentNode
        from agents.tools import available_slugs

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
            # Keep a prompt-only tool even when its slug has no registered
            # handler: the runtime routes it through prompt_tool below.
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
        from chat.models import Message

        if conversation is None:
            return ""
        messages = list(
            conversation.messages.exclude(role=Message.ROLE_SYSTEM).order_by(
                "created_at"
            )
        )
        return self._chat._build_conversation_context(messages)

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
