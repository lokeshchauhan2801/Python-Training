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
import re
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
from chat.services.plain_text import normalize_llm_response

logger = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    message_text: str
    history_text: str  # ORIGINAL conversation history (never mutated)
    pipeline_context: str  # accumulated prior-step output text
    selected_tool: str | None  # last node's router pick (debug/trace only)
    route_reason: str  # last node's reason (debug/trace only)
    visited: list[str]
    hops: int


DIRECT = "__direct__"
_DIRECT_HANDLER_SLUG = "general_assistant"
_WEB_SOURCE_REQUEST = re.compile(
    r"\b(?:web\s*search|online|external\s+(?:source|data|information)|"
    r"current\s+(?:source|data|information|news))\b",
    re.IGNORECASE,
)


def _requests_web_source(message_text: str) -> bool:
    return bool(_WEB_SOURCE_REQUEST.search(message_text or ""))


# Per-slug routing rules; only the rules for a Step's ENABLED tools are shown,
# so the router is never offered a tool it cannot run.
_ROUTER_RULES = {
    "general_assistant": (
        "Choose 'general_assistant' for normal general questions and chart "
        "requests. Attached files do not change this default."
    ),
    "chat_summarise": (
        "Choose 'chat_summarise' whenever the user asks to summarise, recap, or "
        "review this conversation (e.g. 'chat summary', 'summarise this chat', "
        "'give me a summary')."
    ),
    "web_search": (
        "Choose 'web_search' when the user expressly asks for web search, "
        "online, external, or current-source information. This overrides "
        "attached files."
    ),
    "document_search": (
        "Choose 'document_search' only when the user expressly names or "
        "references an attached/uploaded file or document (e.g. 'make a chart "
        "from my CSV', 'chart the figures in this document'). Do not select it "
        "merely because files exist."
    ),
    "add_to_knowledge_base": (
        "Choose 'add_to_knowledge_base' when the user asks to ADD, SAVE, STORE, "
        "UPLOAD, INDEX, or PROCESS an attached file into the knowledge base / "
        "document library / company documents (e.g. 'add this file to the "
        "knowledge base', 'process this document'). This triggers background "
        "Temporal processing (chunk + embed); do NOT use it for merely asking "
        "questions about a file."
    ),
    "document_processing_status": (
        "Choose 'document_processing_status' when the user asks about the "
        "STATUS, PROGRESS, or count of documents being processed, or the status "
        "of a specific document/task id (e.g. 'what's processing right now', "
        "'is my file done', 'status of doc-01H...')."
    ),
}

# Tools that only make sense against this chat's files / processing; they are
# dropped from routing when the chat has files but the message references none.
_FILE_ONLY_SLUGS = frozenset(
    {"document_search", "add_to_knowledge_base", "document_processing_status"}
)

_FALLBACK_ROUTER_PROMPT = (
    "You are a router that selects the single best tool to answer the user's "
    "message.\n"
    "Available tools (slug: description):\n{tools}\n\n"
    "Routing rules:\n{rules}\n\n"
    "Conversation so far:\n{history}\n\n"
    "{attachment_note}\n"
    "User message:\n{question}\n\n"
    "Choose exactly one tool slug from the list above. Respond with the chosen "
    "slug and a short reason."
)


class RouterDecision(PydanticBaseModel):
    """The structured output of the LLM router."""

    tool: str
    reason: str = Field(default="", max_length=300)


class LangGraphChatService:

    def __init__(self, chat_service, langfuse, agent):
        self._chat = chat_service
        self._langfuse = langfuse
        self._agent = agent


    def process_message(
        self,
        user,
        message_text,
        conversation=None,
        company=None,
        attachment_ids=None,
        model_name=None,
        selected_entry_ids=None,
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

        if self._agent is not None and self._agent.company_id != getattr(
            company, "pk", None
        ):
            raise ValueError("Selected agent does not belong to the chat company.")

        llm = self._chat._llm


        history_text = self._history_text(conversation)

        selected_entry_ids = list(selected_entry_ids or [])

        try:
            result = self._run_graph(
                user=user,
                message_text=message_text,
                conversation=conversation,
                company=company,
                attachment_ids=attachment_ids,
                model_name=model_name,
                history_text=history_text,
                agent=self._agent,
                llm=llm,
                selected_entry_ids=selected_entry_ids,
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
        if getattr(tool_result, "llm_generated", False):
            content = normalize_llm_response(content)
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
                    # Tool metadata (file_panel, temporal_links, ...) first so
                    # the fixed keys below always win.
                    **(getattr(tool_result, "metadata", None) or {}),
                    "tool_used": tool_result.tool_used or "",
                    "node_slug": result["entry_slug"],
                    "route_reason": result["route_reason"],
                    "steps": result["steps"],
                    "source": "langgraph",
                    "trace_id": result["trace_id"],
                    # Selected-Agent pipeline provenance (merged last so it wins
                    # over any conflicting tool key).
                    "agent_selection": {
                        "agent_id": str(self._agent.pk),
                        "agent_slug": self._agent.slug,
                        "outcome": "pipeline",
                    },
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
        agent,
        llm,
        selected_entry_ids=None,
    ):
        from agents.services.flow import walk_reachable_steps

        selected_entry_ids = list(selected_entry_ids or [])
        chain = walk_reachable_steps(agent)
        if not chain:
            raise ValueError("Selected agent has no runnable flow.")
        entry_slug = chain[0].slug if chain else ""

        outcome = {
            "tool_result": None,
            "tool_used": "",
            "entry_slug": entry_slug,
            "route_reason": "",
            "trace_id": None,
            "steps": [],
        }

        has_attachment = bool(attachment_ids)
        from chat.services.chat_service import ChatService

        is_chart = ChatService._is_chart_request(message_text or "")
        _files = []
        _file_command = []

        def session_file_list():
            if not _files:
                from chat.services import session_files

                _files.append(
                    session_files.list_files(conversation)
                    if conversation is not None
                    else []
                )
            return _files[0]

        def has_files():
            return has_attachment or bool(session_file_list())

        def command_kind():
            """The message's file-command kind (``Kind.PROCESS`` for a bare
            picker selection), or ``Kind.NONE``."""
            if not _file_command:
                from chat.services import file_commands

                kind = file_commands.classify(message_text).kind
                if selected_entry_ids and not (message_text or "").strip():
                    kind = file_commands.Kind.PROCESS
                _file_command.append(kind)
            return _file_command[0]

        def file_command_requested():
            from chat.services import file_commands

            return command_kind() != file_commands.Kind.NONE

        def command_tool(enabled):
            """The single enabled tool that can run this message's file command,
            or None (no command, or more than one candidate: the router picks).

            PROCESS runs only on ``add_to_knowledge_base`` and STATUS only on
            ``document_processing_status``; STOP / RESUME / LIST run on either.
            The effective kind is used, so a bare "continue" with nothing
            stopped is not a command.
            """
            from chat.services import file_commands

            Kind = file_commands.Kind
            kind = command_kind()
            if kind != Kind.PROCESS and conversation is not None:
                kind = file_commands.effective_kind(
                    file_commands.classify(message_text), conversation
                ) or Kind.NONE
            capable = {
                Kind.PROCESS: ("add_to_knowledge_base",),
                Kind.STATUS: ("document_processing_status",),
                Kind.STOP: ("add_to_knowledge_base", "document_processing_status"),
                Kind.RESUME: ("add_to_knowledge_base", "document_processing_status"),
                Kind.LIST: ("add_to_knowledge_base", "document_processing_status"),
            }.get(kind, ())
            available = [slug for slug in capable if slug in enabled]
            return available[0] if len(available) == 1 else None

        def file_referenced():
            from chat.services import file_matching

            return file_matching.references_file(message_text, session_file_list())

        def _make_node(step):
            """Build a FULL router-executing node for ``step``.

            The closure captures only the IMMUTABLE step row, the ORIGINAL
            history, and the per-turn inputs — NOT pipeline_context /
            effective_history, which are read from ``state`` inside the body at
            run time (capturing them would bake a stale history into the node).
            """

            def _node(state):
                pipeline_context = state.get("pipeline_context", "") or ""
                # The router sees the handoff via effective_history; the handler
                # sees it via ctx.pipeline_context (both from the same source).
                effective_history = history_text
                pc = pipeline_context.strip()
                if pc:
                    effective_history = (
                        f"{history_text}\n\n"
                        f"[Pipeline context from previous steps]\n{pc}"
                    ).strip()

                enabled, tool_rows = self._enabled_for(step)

                def _execute(slug):
                    """Run the handler for ``slug`` -> ToolResult (keying verbatim)."""
                    handler_slug = _DIRECT_HANDLER_SLUG if slug == DIRECT else slug
                    handler = get_handler(handler_slug)
                    tool_row = tool_rows.get(handler_slug)
                    if handler is None:
                        prompt_only = (
                            tool_row is not None and tool_row.type == "langfuse"
                        )
                        handler = get_handler(
                            "prompt_tool" if prompt_only else _DIRECT_HANDLER_SLUG
                        )
                    ctx = ToolContext(
                        user=user,
                        company=company,
                        conversation=conversation,
                        message_text=message_text,
                        history_text=effective_history,
                        attachment_ids=list(attachment_ids or []),
                        model_name=model_name,
                        tool=tool_row,
                        llm=llm,
                        langfuse=self._langfuse,
                        pipeline_context=pipeline_context,
                        selected_entry_ids=list(selected_entry_ids),
                    )
                    result = handler(ctx)
                    if not result.tool_used:
                        result.tool_used = handler_slug
                    return result

                # Precedence: an empty Step answers directly; an explicit web
                # request needs web_search; a file command runs the Step's
                # command tool; a chart naming a chat file reads it
                # with document_search; files that the message does not
                # reference never steer routing; else the strict router picks
                # one of THIS Step's enabled tools.
                trace_id = None
                candidates = enabled
                if not enabled:
                    selected = DIRECT
                    reason = "no tools enabled on the step; answered directly"
                    result = _execute(selected)
                elif _requests_web_source(message_text):
                    if "web_search" in enabled:
                        selected = "web_search"
                        reason = "explicit web-source request"
                        result = _execute(selected)
                    else:
                        selected = "web_search_unavailable"
                        reason = "explicit web-source request but web_search is disabled"
                        result = ToolResult(
                            content="Web search is unavailable for this assistant.",
                            tool_used=selected,
                        )
                elif (command_slug := command_tool(enabled)) is not None:
                    selected = command_slug
                    reason = "file command"
                    result = _execute(selected)
                elif (
                    is_chart
                    and "document_search" in enabled
                    and file_referenced()
                    and not file_command_requested()
                ):
                    selected = "document_search"
                    reason = "explicit file reference"
                    result = _execute(selected)
                else:
                    unreferenced_files = (
                        has_files()
                        and not file_referenced()
                        and not file_command_requested()
                    )
                    if unreferenced_files:
                        candidates = [
                            s for s in enabled if s not in _FILE_ONLY_SLUGS
                        ] or enabled
                    if unreferenced_files and candidates == [_DIRECT_HANDLER_SLUG]:
                        selected = _DIRECT_HANDLER_SLUG
                        reason = "general request without an explicit file reference"
                    else:
                        selected, reason, trace_id = self._route(
                            message_text=message_text,
                            history_text=effective_history,
                            enabled_tools=candidates,
                            step=step,
                            tool_rows=tool_rows,
                            llm=llm,
                            has_attachment=has_attachment,
                        )
                    result = _execute(selected)

                outcome["steps"].append(
                    {"step": step.slug, "tool": result.tool_used}
                )
                outcome["tool_result"] = result
                outcome["tool_used"] = result.tool_used
                outcome["route_reason"] = reason
                outcome["trace_id"] = trace_id

                addition = (
                    f"[Step '{step.slug}' used tool '{result.tool_used}']\n"
                    f"{(result.content or '').strip()}"
                )
                new_context = (
                    f"{pipeline_context}\n\n{addition}".strip()
                    if pipeline_context
                    else addition
                )
                return {
                    "pipeline_context": new_context,
                    "selected_tool": selected,
                    "route_reason": reason,
                    "visited": (state.get("visited") or []) + [step.slug],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node

        app = self._build_graph(
            chain=chain,
            entry_slug=entry_slug,
            make_node=_make_node,
        )

        app.invoke(
            {
                "message_text": message_text,
                "history_text": history_text,
                "pipeline_context": "",
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )

        # The walk is non-empty (validated above) and the orchestrator gate +
        # policy preflight guarantee a runnable chain, so a node always runs.
        return outcome

    def _build_graph(self, *, chain, entry_slug, make_node):
        """Compile a ``StateGraph`` from the linear Step chain.

        Each Step row is a node (ALL run the router — no pass-through nodes);
        edges are START -> entry, step -> next in the chain, and the final step
        -> END (forced for a ``connected_to_end`` step). ``langgraph`` is
        imported HERE (lazily) so importing this module stays SDK-free. The
        chooser's visited check + ``max_hops`` cap guarantee termination even
        under an accidental cycle.
        """
        from langgraph.graph import END, START, StateGraph

        slugs = [s.slug for s in chain]
        node_count = max(len(slugs), 1)
        max_hops = 2 * node_count + 1

        # Linear adjacency from the chain order (deterministic). A
        # connected_to_end step forces END regardless of its position.
        next_of = {}
        for i, step in enumerate(chain):
            nxt = slugs[i + 1] if i + 1 < len(slugs) else END
            next_of[step.slug] = END if step.connected_to_end else nxt

        builder = StateGraph(GraphState)

        for step in chain:
            builder.add_node(step.slug, make_node(step))

        # START -> entry (never route INTO start; entry is a real step).
        entry = entry_slug if entry_slug in slugs else (slugs[0] if slugs else END)
        builder.add_edge(START, entry)

        def _make_choice(slug):
            target = next_of.get(slug, END)

            def _choose(state):
                # Checks in THIS exact precedence:
                #   1. hop cap exceeded       -> END
                #   2. target is END          -> END   (never route OUT of end)
                #   3. target already visited -> END   (drop a cyclic edge)
                #   4. otherwise              -> target (advance one hop)
                if state.get("hops", 0) > max_hops:
                    return END
                if target == END:
                    return END
                if target in (state.get("visited") or []):
                    return END
                return target

            return _choose

        for slug in slugs:
            target = next_of.get(slug, END)
            mapping = {END: END}
            if target in slugs:
                mapping[target] = target
            builder.add_conditional_edges(slug, _make_choice(slug), mapping)

        return builder.compile()


    def _route(
        self,
        *,
        message_text,
        history_text,
        enabled_tools,
        step,
        tool_rows,
        llm,
        has_attachment=False,
    ):
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.services import ChatProviderError

        tools_desc = "\n".join(
            f"{slug}: {self._tool_description(tool_rows.get(slug), slug)}"
            for slug in enabled_tools
        )
        attachment_note = (
            "Attachment note: files are present, but use document_search only when "
            "the user expressly names or references one."
            if has_attachment
            else "Attachment note: no file is attached on this message."
        )
        prompt_name = "agent_router"
        template = self._langfuse.get_prompt(
            prompt_name, fallback=_FALLBACK_ROUTER_PROMPT
        )
        fmt = {
            "question": message_text,
            "tools": tools_desc,
            "history": history_text or "(no prior conversation)",
            "attachment_note": attachment_note,
            "rules": "\n".join(
                f"- {_ROUTER_RULES[slug]}"
                for slug in enabled_tools
                if slug in _ROUTER_RULES
            )
            or "- Choose the tool whose description best fits the message.",
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

        selected = (decision.tool or "").strip()
        reason = (decision.reason or "").strip()
        if selected not in enabled_tools:
            fallback = (
                _DIRECT_HANDLER_SLUG
                if _DIRECT_HANDLER_SLUG in enabled_tools
                else enabled_tools[0]
            )
            logger.info(
                "router returned unavailable tool %r on step %s", selected, step.pk
            )
            return (
                fallback,
                f"router returned unavailable tool '{selected}'; selected '{fallback}'",
                trace_id,
            )
        return (selected, reason, trace_id)


    def _enabled_for(self, step):
        """Return ``(enabled, tool_rows)`` for a single step.

        ``enabled`` is the intersection of the step's attached ``is_enabled``
        tools with slugs that are registry-resolvable OR ``type == "langfuse"``
        (prompt-only). ``tool_rows`` maps every attached tool's raw slug to its
        row. ``enabled`` is de-duplicated in ascending slug order and may be
        empty (the Step then answers directly with ``general_assistant``).
        """
        slugs = available_slugs()
        enabled = []
        seen = set()
        for tool in step.tools.filter(is_enabled=True).order_by("slug"):
            is_prompt_only = tool.type == "langfuse"
            if (tool.slug in slugs or is_prompt_only) and tool.slug not in seen:
                enabled.append(tool.slug)
                seen.add(tool.slug)
        tool_rows = {t.slug: t for t in step.tools.all()}
        return enabled, tool_rows

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
