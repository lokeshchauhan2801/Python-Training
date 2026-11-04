from __future__ import annotations
"""LangGraph chat runtime.

This file holds ONLY the graph implementation: the router prompt, the
``LangGraphChatService`` that compiles a company's agent graph into a
``langgraph`` ``StateGraph``, routes each message to one tool, and persists the
resulting conversation turn.

Multi-agent: a Step may delegate to another Agent (its sub-agent's chain runs
in its place), and a message asking for several things is split by
``chat.services.task_planner`` into ordered sub-tasks, each run by one Step.

The tools themselves (and the registry) live in ``chat/services/llm_tools.py``.
They are imported here and the registry symbols are re-exported so existing
imports of ``chat.services.langgraph_service.available_slugs`` /
``get_handler`` keep working unchanged.
"""

import contextvars
import logging
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import TypedDict

from django.db import connection, transaction
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
from chat.services.timing import timed, with_timing

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
        "from my CSV', 'chart the figures in this document'), or asks a "
        "follow-up about the file the previous turn was answered from (see the "
        "attachment note). Also choose it for questions about the company's "
        "own data or knowledge base: what or how many documents it holds, or "
        "facts found in company documents (e.g. 'how many documents are in "
        "the knowledge base', 'how many employees do we have', 'what does our "
        "leave policy say'). Do not select it merely because files exist."
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
        "STATUS, PROGRESS, or count of documents being processed, the total "
        "number of documents by status, or the status of a specific "
        "document/task id (e.g. 'what's processing right now', 'is my file "
        "done', 'status of doc-01H...')."
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


class _StreamingLLM:
    """``LLMClient`` proxy whose ``complete()`` streams its answer.

    Each text piece goes to ``on_delta`` and the joined text is returned, so
    callers see the same result as ``complete()``. Everything else
    (``generate_structured``, embeddings, ``provider`` / ``model``) goes
    straight to the wrapped client. Handed only to the chain's last step.
    """

    def __init__(self, llm, on_delta):
        self._llm = llm
        self._on_delta = on_delta

    def complete(self, messages, **kwargs):
        if kwargs:
            # stream() takes no sampling overrides; keep them exact.
            return self._llm.complete(messages, **kwargs)
        parts = []
        for piece in self._llm.stream(messages):
            parts.append(piece)
            self._on_delta(piece)
        return "".join(parts)

    def __getattr__(self, name):
        return getattr(self._llm, name)


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
        timing=None,
    ):
        """Route one message through the graph and persist one turn.

        Returns the created ``Conversation`` turn (same as ``ChatService``).
        ``timing`` is an optional ``TurnTimer``; its summary is saved under
        ``metadata["timing"]`` (``None`` only logs the stages).
        Raises only the exception types ``chat.views.send`` already catches
        (``LLMConfigurationError``, ``LLMBackendError``, ``ChatProviderError``,
        ``ChatRetrievalError``) so the view's HTTP status mapping is unchanged.
        """
        self._check_turn(user, conversation, company)
        llm = self._chat._llm


        with timed(timing, "history"):
            history_text, router_history_text = self._history_texts(conversation)

        selected_entry_ids = list(selected_entry_ids or [])

        result = self._run_graph_mapped(
            user=user,
            message_text=message_text,
            conversation=conversation,
            company=company,
            attachment_ids=attachment_ids,
            model_name=model_name,
            history_text=history_text,
            router_history_text=router_history_text,
            agent=self._agent,
            llm=llm,
            selected_entry_ids=selected_entry_ids,
            timing=timing,
        )
        return self._persist(
            result,
            user=user,
            message_text=message_text,
            conversation=conversation,
            attachment_ids=attachment_ids,
            model_name=model_name,
            llm=llm,
            timing=timing,
        )

    def stream_message(
        self,
        user,
        message_text,
        conversation=None,
        company=None,
        attachment_ids=None,
        model_name=None,
        selected_entry_ids=None,
        timing=None,
    ):
        """Streaming ``process_message``: a generator of ``(event, value)``.

        The graph runs in a worker thread. Only the chain's last step gets a
        streaming LLM, so ``("delta", text)`` items are that step's answer
        pieces (the router and earlier steps never stream); a multi-task turn
        sends each finished sub-task's answer as one piece. Ends with
        ``("done", turn)``; the saved turn and the errors raised are the same
        as ``process_message``.
        """
        self._check_turn(user, conversation, company)
        llm = self._chat._llm

        with timed(timing, "history"):
            history_text, router_history_text = self._history_texts(conversation)

        selected_entry_ids = list(selected_entry_ids or [])
        events = queue.Queue()

        def _on_delta(piece):
            if timing is not None:
                timing.mark_first_token()
            events.put(("delta", piece))

        def _worker():
            try:
                events.put((
                    "result",
                    self._run_graph_mapped(
                        user=user,
                        message_text=message_text,
                        conversation=conversation,
                        company=company,
                        attachment_ids=attachment_ids,
                        model_name=model_name,
                        history_text=history_text,
                        router_history_text=router_history_text,
                        agent=self._agent,
                        llm=llm,
                        on_delta=_on_delta,
                        selected_entry_ids=selected_entry_ids,
                        timing=timing,
                    ),
                ))
            except BaseException as exc:
                events.put(("error", exc))
            finally:
                # The worker thread has its own DB connection; don't leak it.
                connection.close()

        # copy_context keeps the request's current user (audit actor).
        worker = threading.Thread(
            target=contextvars.copy_context().run, args=(_worker,), daemon=True
        )
        worker.start()
        # A close() at a delta yield (client disconnect) must not lose the
        # turn: keep draining until the worker's result, then persist as the
        # JSON path would, without yielding anything more.
        disconnected = False
        try:
            while True:
                kind, value = events.get()
                if kind == "delta":
                    if disconnected:
                        continue
                    try:
                        yield ("delta", value)
                    except GeneratorExit:
                        disconnected = True
                        logger.info(
                            "Agent chat stream closed by client; "
                            "finishing the turn"
                        )
                elif kind == "error":
                    raise value
                else:
                    result = value
                    break
            worker.join()
            turn = self._persist(
                result,
                user=user,
                message_text=message_text,
                conversation=conversation,
                attachment_ids=attachment_ids,
                model_name=model_name,
                llm=llm,
                timing=timing,
            )
        except Exception:
            if not disconnected:
                raise
            logger.exception(
                "Agent chat turn failed after the client disconnected"
            )
            return
        if not disconnected:
            yield ("done", turn)

    def _check_turn(self, user, conversation, company):
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

    def _run_graph_mapped(self, **kwargs):
        """``_run_graph`` with errors mapped to the types the view catches."""
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.services import ChatProviderError, ChatRetrievalError

        try:
            return self._run_graph(**kwargs)
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

    def _persist(
        self,
        result,
        *,
        user,
        message_text,
        conversation,
        attachment_ids,
        model_name,
        llm,
        timing=None,
    ):
        """Post-process the graph ``result`` and save it as one turn."""
        from chat.retitle import retitle_if_default
        from chat.services import ChatProviderError

        tool_result = result["tool_result"]
        content = (tool_result.content or "").strip()
        if getattr(tool_result, "llm_generated", False):
            content = normalize_llm_response(content)
        if not content:
            raise ChatProviderError("The LLM provider returned an empty response.")


        assistant_model_name = model_name or getattr(llm, "model", None)


        # Timing is measured up to here (persist is only logged).
        with timed(None, "persist"), transaction.atomic():
            convo = conversation
            turn = self._chat.create_turn(
                convo,
                user=user,
                question=message_text,
                response=content,
                model_name=assistant_model_name,
                citations=tool_result.citations or [],
                chart_data=tool_result.chart_data,
                metadata=with_timing({
                    # Tool metadata (file_panel, temporal_links, ...) first so
                    # the fixed keys below always win.
                    **(getattr(tool_result, "metadata", None) or {}),
                    "tool_used": tool_result.tool_used or "",
                    "node_slug": result["entry_slug"],
                    "route_reason": result["route_reason"],
                    "steps": result["steps"],
                    # Multi-task turns: one entry per planned sub-task.
                    **({"tasks": result["tasks"]} if result.get("tasks") else {}),
                    "source": "langgraph",
                    "trace_id": result["trace_id"],
                    # Selected-Agent pipeline provenance (merged last so it wins
                    # over any conflicting tool key).
                    "agent_selection": {
                        "agent_id": str(self._agent.pk),
                        "agent_slug": self._agent.slug,
                        "outcome": "pipeline",
                    },
                }, timing),
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
        router_history_text=None,
        timing=None,
        on_delta=None,
    ):
        """Run one turn through ``agent``'s flow -> the outcome ``_persist`` saves.

        A message that asks for several things is split by the task planner
        into ordered sub-tasks, each run by one worker Step (its own tools or a
        delegated sub-agent). Any other message runs the Step chain. Streamed
        answer text goes to ``on_delta``.
        """
        from agents.services.flow import walk_reachable_steps
        from chat.services import task_planner

        chain = walk_reachable_steps(agent)
        if not chain:
            raise ValueError("Selected agent has no runnable flow.")

        runner = _TurnRunner(
            self,
            user=user,
            conversation=conversation,
            company=company,
            attachment_ids=list(attachment_ids or []),
            model_name=model_name,
            history_text=history_text,
            # The router sees only the last CHAT_ROUTER_TURNS turns; the tool
            # handlers still get the full (capped) history.
            router_history_text=(
                history_text if router_history_text is None else router_history_text
            ),
            llm=llm,
            selected_entry_ids=list(selected_entry_ids or []),
            timing=timing,
        )

        tasks = []
        picker_only = runner.selected_entry_ids and not (message_text or "").strip()
        if not picker_only and task_planner.looks_multi_task(message_text):
            with timed(timing, "planner"):
                tasks = task_planner.plan_tasks(
                    llm=llm,
                    langfuse=self._langfuse,
                    message_text=message_text,
                    history_text=runner.router_history_text,
                    workers=runner.workers(chain),
                    file_titles=[f.title for f in runner.session_file_list()],
                )
        if tasks:
            return runner.run_tasks(agent, tasks, on_delta=on_delta)

        # Only the chain's last step gets a streaming LLM (``run_chain``).
        stream_llm = _StreamingLLM(llm, on_delta) if on_delta is not None else None
        run = runner.run_chain(
            agent, message_text, "", stream_llm=stream_llm, chain=chain
        )
        # The walk is non-empty (validated above) and the orchestrator gate +
        # policy preflight guarantee a runnable chain, so a node always runs.
        return {
            "tool_result": run.result,
            "tool_used": run.result.tool_used,
            "entry_slug": chain[0].slug,
            "route_reason": run.reason,
            "trace_id": run.trace_id,
            "steps": run.records,
        }

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
        follows_file_turn=False,
        timing=None,
    ):
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.services import ChatProviderError

        tools_desc = "\n".join(
            f"{slug}: {self._tool_description(tool_rows.get(slug), slug)}"
            for slug in enabled_tools
        )
        if follows_file_turn and "document_search" in enabled_tools:
            attachment_note = (
                "Attachment note: the previous turn was answered from a file in "
                "this chat. If this message is a follow-up about that file or "
                "its contents (e.g. 'and the aadhaar number?', 'what about his "
                "PAN?'), choose document_search. If it is about an unrelated "
                "topic, choose general_assistant."
            )
        elif has_attachment:
            attachment_note = (
                "Attachment note: files are present, but use document_search only when "
                "the user expressly names or references one."
            )
        else:
            attachment_note = "Attachment note: no file is attached on this message."
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
                with timed(timing, "router"):
                    decision = llm.generate_structured(
                        router_prompt, RouterDecision
                    )
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

    def _history_texts(self, conversation):
        """Return ``(history_text, router_history_text)`` from one turns query.

        ``history_text`` is the full condensed context the tools get;
        ``router_history_text`` covers only the last ``CHAT_ROUTER_TURNS``
        turns, which is all the router needs to pick a tool.
        """
        from django.conf import settings

        if conversation is None:
            return "", ""
        turns = list(conversation.turns.order_by("created_at"))
        router_turns = max(int(getattr(settings, "CHAT_ROUTER_TURNS", 4)), 0)
        return (
            self._chat._build_conversation_context(turns),
            self._chat._build_conversation_context(
                turns[-router_turns:] if router_turns else []
            ),
        )

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


@dataclass
class _StepRun:
    """What running one Step (or a whole chain) produced."""

    result: ToolResult
    selected: str = ""
    reason: str = ""
    trace_id: str | None = None
    records: list = field(default_factory=list)


class _MessageSignals:
    """Cached facts about ONE message that the Step precedence reads.

    The message is the turn's own text, or a planned sub-task's instruction.
    """

    def __init__(
        self,
        message_text,
        *,
        conversation,
        attachment_ids,
        selected_entry_ids,
        session_file_list,
    ):
        from chat.services.chat_service import ChatService

        self.message_text = message_text or ""
        self.has_attachment = bool(attachment_ids)
        self.is_chart = ChatService._is_chart_request(self.message_text)
        self.web_request = _requests_web_source(self.message_text)
        self._conversation = conversation
        self._selected_entry_ids = selected_entry_ids
        self._session_file_list = session_file_list
        self._kind = None
        self._follow_up = None

    def has_files(self):
        return self.has_attachment or bool(self._session_file_list())

    def command_kind(self):
        """The message's file-command kind (``Kind.PROCESS`` for a bare
        picker selection), or ``Kind.NONE``."""
        if self._kind is None:
            from chat.services import file_commands

            kind = file_commands.classify(self.message_text).kind
            if self._selected_entry_ids and not self.message_text.strip():
                kind = file_commands.Kind.PROCESS
            self._kind = kind
        return self._kind

    def file_command_requested(self):
        from chat.services import file_commands

        return self.command_kind() != file_commands.Kind.NONE

    def command_tool(self, enabled):
        """The single enabled tool that can run this message's file command,
        or None (no command, or more than one candidate: the router picks).

        PROCESS runs only on ``add_to_knowledge_base`` and STATUS only on
        ``document_processing_status``; STOP / RESUME / LIST run on either.
        The effective kind is used, so a bare "continue" with nothing
        stopped is not a command.
        """
        from chat.services import file_commands

        Kind = file_commands.Kind
        kind = self.command_kind()
        if kind != Kind.PROCESS and self._conversation is not None:
            kind = file_commands.effective_kind(
                file_commands.classify(self.message_text), self._conversation
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

    def follows_file_turn(self):
        """The previous turn was answered from a file (router decides
        whether this message is a follow-up about it)."""
        if self._follow_up is None:
            from chat.services.chat_service import ChatService

            self._follow_up = ChatService._follows_file_turn(
                self._conversation, self.message_text
            )
        return self._follow_up

    def file_referenced(self):
        from chat.services import file_matching

        return file_matching.references_file(
            self.message_text, self._session_file_list()
        ) or self.follows_file_turn()


class _TurnRunner:
    """One turn's inputs plus the Step, sub-agent and task executors.

    A delegated sub-agent and a planned sub-task run the very same Step
    precedence as the selected Agent's own chain; only the message text and
    the pipeline context differ.
    """

    def __init__(
        self,
        service,
        *,
        user,
        conversation,
        company,
        attachment_ids,
        model_name,
        history_text,
        router_history_text,
        llm,
        selected_entry_ids,
        timing,
    ):
        self._service = service
        self.user = user
        self.conversation = conversation
        self.company = company
        self.attachment_ids = attachment_ids
        self.model_name = model_name
        self.history_text = history_text
        self.router_history_text = router_history_text
        self.llm = llm
        self.selected_entry_ids = selected_entry_ids
        self.timing = timing
        self._files = None
        self._signals = {}

    def session_file_list(self):
        if self._files is None:
            from chat.services import session_files

            self._files = (
                session_files.list_files(self.conversation)
                if self.conversation is not None
                else []
            )
        return self._files

    def signals(self, message_text):
        key = message_text or ""
        if key not in self._signals:
            self._signals[key] = _MessageSignals(
                key,
                conversation=self.conversation,
                attachment_ids=self.attachment_ids,
                selected_entry_ids=self.selected_entry_ids,
                session_file_list=self.session_file_list,
            )
        return self._signals[key]

    def workers(self, chain):
        """The planner's workers: each chain Step with the tools it can run
        (a delegating Step offers its sub-agent's tools)."""
        from agents.services.flow import delegate_of, walk_tool_steps
        from chat.services.task_planner import Worker

        workers = []
        for step in chain:
            sub = delegate_of(step)
            steps = walk_tool_steps(sub) if sub is not None else [step]
            tools = sorted(
                {slug for s in steps for slug in self._service._enabled_for(s)[0]}
            )
            workers.append(Worker(key=step.slug, step=step, delegate=sub, tools=tools))
        return workers

    # ------------------------------------------------------------------ #
    # Executors
    # ------------------------------------------------------------------ #
    def run_chain(
        self,
        agent,
        message_text,
        pipeline_context,
        *,
        stream_llm=None,
        tool_hint="",
        uses_previous=False,
        depth=0,
        seen=(),
        chain=None,
    ):
        """Run ``agent``'s Step chain as a LangGraph for ``message_text``.

        Each Step's output is appended to the pipeline context the next Step
        sees; the last Step's run is the chain's result.
        """
        from agents.services.flow import walk_reachable_steps

        if chain is None:
            chain = walk_reachable_steps(agent)
        seen = tuple(seen) + (agent.pk,)
        # Steps that end the walk (see ``_build_graph``): only their handler
        # may stream. Repeated slugs make that unclear, so nothing streams.
        chain_slugs = [s.slug for s in chain]
        last_slugs = (
            {
                s.slug
                for i, s in enumerate(chain)
                if s.connected_to_end or i == len(chain) - 1
            }
            if stream_llm is not None and len(set(chain_slugs)) == len(chain_slugs)
            else set()
        )
        records = []
        last = []

        def _make_node(step):
            def _node(state):
                context = state.get("pipeline_context", "") or ""
                run = self.run_step(
                    step,
                    message_text,
                    context,
                    stream_llm=stream_llm if step.slug in last_slugs else None,
                    tool_hint=tool_hint,
                    uses_previous=uses_previous,
                    depth=depth,
                    seen=seen,
                )
                records.extend(run.records)
                last[:] = [run]
                addition = (
                    f"[Step '{step.slug}' used tool '{run.result.tool_used}']\n"
                    f"{(run.result.content or '').strip()}"
                )
                return {
                    "pipeline_context": (
                        f"{context}\n\n{addition}".strip() if context else addition
                    ),
                    "selected_tool": run.selected,
                    "route_reason": run.reason,
                    "visited": (state.get("visited") or []) + [step.slug],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node

        app = self._service._build_graph(
            chain=chain, entry_slug=chain[0].slug, make_node=_make_node
        )
        app.invoke(
            {
                "message_text": message_text,
                "history_text": self.history_text,
                "pipeline_context": pipeline_context or "",
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )
        run = last[0]
        return _StepRun(run.result, run.selected, run.reason, run.trace_id, records)

    def run_step(
        self,
        step,
        message_text,
        pipeline_context,
        *,
        stream_llm=None,
        tool_hint="",
        uses_previous=False,
        depth=0,
        seen=(),
    ):
        """Run ONE Step for ``message_text`` -> ``_StepRun``.

        A delegating Step runs its sub-agent's whole chain instead. Past the
        depth / cycle guard, or when the sub-agent has no runnable flow, the
        Step answers with its own tools.
        """
        from agents.services.flow import (
            MAX_DELEGATION_DEPTH,
            delegate_of,
            walk_reachable_steps,
        )

        sub = delegate_of(step)
        if sub is not None and sub.pk not in seen and depth < MAX_DELEGATION_DEPTH:
            sub_chain = walk_reachable_steps(sub)
            if sub_chain:
                run = self.run_chain(
                    sub,
                    message_text,
                    pipeline_context,
                    stream_llm=stream_llm,
                    tool_hint=tool_hint,
                    uses_previous=uses_previous,
                    depth=depth + 1,
                    seen=seen,
                    chain=sub_chain,
                )
                for record in run.records:
                    record.setdefault("agent", sub.slug)
                    record.setdefault("via", step.slug)
                return run
            logger.info("delegate agent %s of step %s has no runnable flow", sub.pk, step.pk)

        signals = self.signals(message_text)
        # The router sees the handoff via router_history (trimmed to the last
        # CHAT_ROUTER_TURNS turns); the handler sees it via effective_history +
        # ctx.pipeline_context (same source).
        effective_history = self.history_text
        router_history = self.router_history_text
        pc = (pipeline_context or "").strip()
        if pc:
            handoff = f"[Pipeline context from previous steps]\n{pc}"
            effective_history = f"{self.history_text}\n\n{handoff}".strip()
            router_history = f"{self.router_history_text}\n\n{handoff}".strip()

        enabled, tool_rows = self._service._enabled_for(step)
        # The router always gets ``llm`` (never streams); a final step's
        # handler gets the streaming proxy when streaming.
        step_llm = stream_llm or self.llm

        def _execute(slug):
            """Run the handler for ``slug`` -> ToolResult (keying verbatim)."""
            handler_slug = _DIRECT_HANDLER_SLUG if slug == DIRECT else slug
            handler = get_handler(handler_slug)
            tool_row = tool_rows.get(handler_slug)
            if handler is None:
                prompt_only = tool_row is not None and tool_row.type == "langfuse"
                handler = get_handler(
                    "prompt_tool" if prompt_only else _DIRECT_HANDLER_SLUG
                )
            ctx = ToolContext(
                user=self.user,
                company=self.company,
                conversation=self.conversation,
                message_text=message_text,
                history_text=effective_history,
                attachment_ids=list(self.attachment_ids),
                model_name=self.model_name,
                tool=tool_row,
                llm=step_llm,
                langfuse=self._service._langfuse,
                pipeline_context=pipeline_context or "",
                selected_entry_ids=list(self.selected_entry_ids),
                uses_prior_results=uses_previous,
            )
            # A streamed last-step answer marks time-to-first-token on its
            # first piece (``stream_message``).
            with timed(self.timing, "llm"):
                result = handler(ctx)
            if not result.tool_used:
                result.tool_used = handler_slug
            return result

        # Precedence: an empty Step answers directly; an explicit web request
        # needs web_search; a file command runs the Step's command tool; a
        # planned task's tool runs when this Step has it; a chart naming a chat
        # file reads it with document_search; files that the message does not
        # reference never steer routing; else the strict router picks one of
        # THIS Step's enabled tools.
        trace_id = None
        candidates = enabled
        if not enabled:
            selected = DIRECT
            reason = "no tools enabled on the step; answered directly"
            result = _execute(selected)
        elif signals.web_request:
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
        elif (command_slug := signals.command_tool(enabled)) is not None:
            selected = command_slug
            reason = "file command"
            result = _execute(selected)
        elif tool_hint and tool_hint in enabled:
            selected = tool_hint
            reason = "task plan"
            result = _execute(selected)
        elif (
            signals.is_chart
            and "document_search" in enabled
            and signals.file_referenced()
            and not signals.file_command_requested()
        ):
            selected = "document_search"
            reason = "explicit file reference"
            result = _execute(selected)
        elif (
            signals.has_attachment
            and "document_search" in enabled
            and not signals.file_command_requested()
        ):
            # A file attached on THIS turn is referenced by the message itself
            # (as on the Normal chat path), even when the text never says
            # "file" ("can you recheck and give the PAN").
            selected = "document_search"
            reason = "file attached on this turn"
            result = _execute(selected)
        else:
            unreferenced_files = (
                signals.has_files()
                and not signals.file_referenced()
                and not signals.file_command_requested()
            )
            if unreferenced_files:
                candidates = [s for s in enabled if s not in _FILE_ONLY_SLUGS] or enabled
            if unreferenced_files and candidates == [_DIRECT_HANDLER_SLUG]:
                selected = _DIRECT_HANDLER_SLUG
                reason = "general request without an explicit file reference"
            else:
                selected, reason, trace_id = self._service._route(
                    message_text=message_text,
                    history_text=router_history,
                    enabled_tools=candidates,
                    step=step,
                    tool_rows=tool_rows,
                    llm=self.llm,
                    has_attachment=signals.has_attachment,
                    follows_file_turn=signals.follows_file_turn(),
                    timing=self.timing,
                )
            result = _execute(selected)

        return _StepRun(
            result,
            selected,
            reason,
            trace_id,
            [{"step": step.slug, "tool": result.tool_used}],
        )

    def run_tasks(self, agent, tasks, *, on_delta=None):
        """Run planned sub-tasks in order as a LangGraph -> the turn outcome.

        A task marked ``uses_previous`` sees every earlier task's output as
        pipeline context ("make a pie chart of those statuses"). Each finished
        task's answer is streamed as soon as it is ready.
        """
        from langgraph.graph import END, START, StateGraph

        runs = []

        def _make_node(index, task):
            def _node(state):
                context = state.get("pipeline_context", "") or ""
                run = self.run_step(
                    task.worker.step,
                    task.instruction,
                    context if task.uses_previous else "",
                    tool_hint=task.tool,
                    uses_previous=task.uses_previous,
                    seen=(agent.pk,),
                )
                runs.append(run)
                if on_delta is not None:
                    on_delta(("\n\n" if index else "") + _task_section(index, task, run.result))
                addition = (
                    f"[Task {index + 1} '{task.title or task.instruction}' used tool "
                    f"'{run.result.tool_used}']\n{(run.result.content or '').strip()}"
                )
                return {
                    "pipeline_context": (
                        f"{context}\n\n{addition}".strip() if context else addition
                    ),
                    "selected_tool": run.selected,
                    "route_reason": run.reason,
                    "visited": (state.get("visited") or []) + [f"task_{index + 1}"],
                    "hops": state.get("hops", 0) + 1,
                }

            return _node

        names = [f"task_{i + 1}" for i in range(len(tasks))]
        builder = StateGraph(GraphState)
        for index, (name, task) in enumerate(zip(names, tasks)):
            builder.add_node(name, _make_node(index, task))
        builder.add_edge(START, names[0])
        for source, target in zip(names, names[1:]):
            builder.add_edge(source, target)
        builder.add_edge(names[-1], END)
        builder.compile().invoke(
            {
                "message_text": "",
                "history_text": self.history_text,
                "pipeline_context": "",
                "selected_tool": None,
                "route_reason": "",
                "visited": [],
                "hops": 0,
            }
        )

        combined = _combine_task_results(tasks, [run.result for run in runs])
        return {
            "tool_result": combined,
            "tool_used": combined.tool_used,
            "entry_slug": tasks[0].worker.key,
            "route_reason": f"multi-task plan ({len(tasks)} tasks)",
            "trace_id": next((run.trace_id for run in runs if run.trace_id), None),
            "steps": [
                {**record, "task": index + 1}
                for index, run in enumerate(runs)
                for record in run.records
            ],
            "tasks": [
                {
                    "title": task.title,
                    "instruction": task.instruction,
                    "worker": task.worker.key,
                    "agent": (task.worker.delegate or agent).slug,
                    "tool": run.result.tool_used,
                    "uses_previous": task.uses_previous,
                }
                for task, run in zip(tasks, runs)
            ],
        }


def _task_section(index, task, result):
    """One numbered block of a multi-task answer."""
    content = (result.content or "").strip()
    if getattr(result, "llm_generated", False):
        content = normalize_llm_response(content)
    return f"{index + 1}. {task.title or task.instruction}\n{content}"


def _combine_task_results(tasks, results):
    """Merge the per-task ToolResults into the one turn the chat saves.

    Text is numbered per task (each LLM answer already normalized), citations
    are concatenated, the last chart wins and metadata merges in task order
    (list values are concatenated, e.g. ``temporal_links``).
    """
    citations = []
    chart_data = None
    metadata = {}
    for result in results:
        citations.extend(result.citations or [])
        if result.chart_data is not None:
            chart_data = result.chart_data
        for key, value in (result.metadata or {}).items():
            if isinstance(value, list) and isinstance(metadata.get(key), list):
                metadata[key] = metadata[key] + value
            else:
                metadata[key] = value
    used = list(dict.fromkeys(r.tool_used for r in results if r.tool_used))
    return ToolResult(
        content="\n\n".join(
            _task_section(i, task, result)
            for i, (task, result) in enumerate(zip(tasks, results))
        ),
        citations=citations,
        chart_data=chart_data,
        tool_used=",".join(used),
        metadata=metadata,
    )
