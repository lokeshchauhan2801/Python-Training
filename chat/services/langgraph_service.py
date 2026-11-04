from __future__ import annotations
"""LangGraph chat runtime.

This file holds ONLY the graph implementation: the router prompt, the
``LangGraphChatService`` that compiles a company's agent graph into a
``langgraph`` ``StateGraph``, routes each message to one tool, and persists the
resulting conversation turn.

Multi-agent (supervisor): an Agent's Steps are its workers. For each message
the supervisor (``chat.services.task_planner``) splits the request into parts
and gives each part to one worker; a worker runs its own tools, hands the part
to another Agent (which supervises its own workers), or, with no tools,
answers with the LLM in its role. Parts that need nothing from each other run
at the same time; a part that needs another's answer waits for it.

The tools themselves (and the registry) live in ``chat/services/llm_tools.py``.
They are imported here and the registry symbols are re-exported so existing
imports of ``chat.services.langgraph_service.available_slugs`` /
``get_handler`` keep working unchanged.
"""

import contextvars
import logging
import queue
import re
import operator
import threading
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

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
    # Numbers of the finished parts; parts running in parallel append at once.
    done: Annotated[list, operator.add]


DIRECT = "__direct__"
# ``tool_used`` of a worker with no tools that answered with the LLM itself.
LLM_ANSWER = "llm_answer"
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

# Tools that work on what earlier steps / agents produced.
_ROUTER_RULES.update({
    "report_writer": (
        "Choose 'report_writer' when the user asks to write up, report on or "
        "turn results into a report."
    ),
    "action_items": (
        "Choose 'action_items' when the user asks for action items, tasks, "
        "to-dos or next steps from some content."
    ),
    "email_drafter": (
        "Choose 'email_drafter' when the user asks to write, draft or compose an "
        "email or message to someone."
    ),
    "translator": (
        "Choose 'translator' when the user asks to translate something into "
        "another language."
    ),
    "chart_builder": (
        "Choose 'chart_builder' when the user asks for a chart or graph of "
        "results or figures from earlier in this request."
    ),
})

# Tools with side effects (they start background processing).
_ACTION_SLUGS = frozenset({"add_to_knowledge_base"})

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
    straight to the wrapped client. Handed only to a single-part turn's worker.
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

        The graph runs in a worker thread. A single-part turn's worker gets a
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
                        "agent_name": self._agent.name or self._agent.slug,
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
        """Run one turn with ``agent`` as supervisor -> the outcome ``_persist`` saves.

        The supervisor splits the message into parts, gives each to a worker
        and runs them (independent parts in parallel). Streamed answer text
        goes to ``on_delta``.
        """
        from agents.services.flow import agent_workers

        steps = agent_workers(agent)
        if not steps:
            raise ValueError("Selected agent has no workers.")

        runner = _TurnRunner(
            self,
            user=user,
            conversation=conversation,
            company=company,
            attachment_ids=list(attachment_ids or []),
            model_name=model_name,
            history_text=history_text,
            # The supervisor and router see only the last CHAT_ROUTER_TURNS
            # turns; the tool handlers still get the full (capped) history.
            router_history_text=(
                history_text if router_history_text is None else router_history_text
            ),
            llm=llm,
            selected_entry_ids=list(selected_entry_ids or []),
            timing=timing,
        )
        tasks = runner.supervise(message_text, steps)
        return runner.run_tasks(agent, tasks, on_delta=on_delta)

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
    """What running one worker (or a supervised agent) produced."""

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
    precedence as the selected Agent's own workers; only the message text and
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

    def tool_rules(self, workers):
        """The router rules for every tool the supervisor's workers can run."""
        slugs = {slug for w in workers for slug in w.tools}
        return {s: _ROUTER_RULES[s] for s in sorted(slugs) if s in _ROUTER_RULES}

    def workers(self, steps):
        """The supervisor's workers: each Step with the tools it can run (a
        hand-off Step offers its agent's tools)."""
        from agents.services.flow import delegate_of, walk_tool_steps
        from chat.services.task_planner import Worker

        workers = []
        for step in steps:
            sub = delegate_of(step)
            tool_steps = walk_tool_steps(sub) if sub is not None else [step]
            tools = sorted(
                {slug for s in tool_steps for slug in self._service._enabled_for(s)[0]}
            )
            workers.append(Worker(key=step.slug, step=step, delegate=sub, tools=tools))
        return workers

    def fallback_worker(self, workers, message_text):
        """The worker for the whole message when there is no plan: the one
        that can run its file command, else a general one, else the first."""
        signals = self.signals(message_text)
        if signals.file_command_requested():
            for w in workers:
                if signals.command_tool(w.tools):
                    return w
        for w in workers:
            if _DIRECT_HANDLER_SLUG in w.tools or not w.tools:
                return w
        return workers[0]

    def supervise(self, message_text, steps):
        """The supervisor's plan for ``message_text`` over ``steps`` (>= 1 part).

        One worker and a single request: no planner call. A bare file-picker
        selection goes to the worker that adds files to the knowledge base.
        """
        from chat.services import task_planner

        workers = self.workers(steps)
        if self.selected_entry_ids and not (message_text or "").strip():
            worker = next(
                (w for w in workers if "add_to_knowledge_base" in w.tools),
                workers[0],
            )
            return [
                task_planner.PlannedTask(
                    instruction=message_text or "",
                    worker=worker,
                    tool="add_to_knowledge_base",
                    title="Process files",
                )
            ]
        tasks = []
        if len(workers) > 1 or task_planner.looks_multi_task(message_text):
            with timed(self.timing, "planner"):
                tasks = task_planner.plan_tasks(
                    llm=self.llm,
                    langfuse=self._service._langfuse,
                    message_text=message_text,
                    history_text=self.router_history_text,
                    workers=workers,
                    file_titles=[f.title for f in self.session_file_list()],
                    tool_rules=self.tool_rules(workers),
                )
        return tasks or [
            task_planner.PlannedTask(
                instruction=message_text or "",
                worker=self.fallback_worker(workers, message_text),
            )
        ]

    def run_agent(
        self,
        agent,
        message_text,
        context,
        *,
        stream_llm=None,
        tool_hint="",
        uses_previous=False,
        depth=0,
        seen=(),
    ):
        """Run a part handed to ``agent`` -> ``_StepRun`` (``None``: no workers).

        One worker, or one that has the planned tool, runs it directly;
        otherwise the agent supervises its own workers for this part.
        """
        from agents.services.flow import agent_workers

        steps = agent_workers(agent)
        if not steps:
            return None
        workers = self.workers(steps)
        chosen = workers[0] if len(workers) == 1 else next(
            (w for w in workers if tool_hint and tool_hint in w.tools), None
        )
        if chosen is not None:
            return self.run_step(
                chosen.step,
                message_text,
                context,
                stream_llm=stream_llm,
                tool_hint=tool_hint,
                uses_previous=uses_previous,
                depth=depth,
                seen=seen,
            )
        outcome = self.run_tasks(
            agent,
            self.supervise(message_text, steps),
            base_context=context,
            depth=depth,
            seen=seen,
        )
        return _StepRun(
            outcome["tool_result"],
            outcome["tool_used"],
            outcome["route_reason"],
            outcome["trace_id"],
            outcome["steps"],
        )

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
        """Run ONE worker Step for ``message_text`` -> ``_StepRun``.

        A hand-off Step gives the part to its agent instead (``run_agent``).
        Past the depth / cycle guard, or when that agent has no workers, the
        Step answers with its own tools.
        """
        from agents.services.flow import MAX_DELEGATION_DEPTH, delegate_of

        sub = delegate_of(step)
        if sub is not None and sub.pk not in seen and depth < MAX_DELEGATION_DEPTH:
            run = self.run_agent(
                sub,
                message_text,
                pipeline_context,
                stream_llm=stream_llm,
                tool_hint=tool_hint,
                uses_previous=uses_previous,
                depth=depth + 1,
                seen=tuple(seen) + (sub.pk,),
            )
            if run is not None:
                for record in run.records:
                    record.setdefault("agent", sub.slug)
                    record.setdefault("agent_name", sub.name or sub.slug)
                    record.setdefault("via", step.slug)
                    record.setdefault("via_name", step.name or step.slug)
                return run
            logger.info("delegate agent %s of step %s has no workers", sub.pk, step.pk)

        signals = self.signals(message_text)
        # The router sees the handoff via router_history (trimmed to the last
        # CHAT_ROUTER_TURNS turns); the handler sees it via effective_history +
        # ctx.pipeline_context (same source).
        effective_history = self.history_text
        router_history = self.router_history_text
        pc = (pipeline_context or "").strip()
        if pc:
            handoff = f"[Answers from the other agents for this message]\n{pc}"
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
                persona=_persona(step),
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
            reason = "no tools: the worker answered with the LLM in its role"
            result = _execute(selected)
            if not result.chart_data:
                result.tool_used = LLM_ANSWER
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
        elif (
            tool_hint
            and tool_hint in enabled
            # The plan never starts processing on its own: that needs the task
            # text itself to be a file command (handled just above).
            and tool_hint not in _ACTION_SLUGS
        ):
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
            [{"step": step.slug, "step_name": step.name or step.slug, "tool": result.tool_used}],
        )

    def run_tasks(self, agent, tasks, *, on_delta=None, base_context="", depth=0, seen=()):
        """Run the supervisor's parts as a LangGraph -> the turn outcome.

        Each part is a node. A part with ``depends_on`` gets an edge from
        each part it needs (it waits for all of them and receives only their
        answers); the others start at once, so independent parts run at the
        same time. The reply lists the parts in the order they were asked.
        """
        from django.db import connection
        from langgraph.graph import END, START, StateGraph

        from chat.services.task_planner import waves

        runs = {}
        single = len(tasks) == 1
        # A single part streams its own answer like a normal turn; parts in
        # parallel send each finished answer as one piece.
        stream_llm = (
            _StreamingLLM(self.llm, on_delta) if single and on_delta is not None else None
        )
        origin = threading.get_ident()
        seen = tuple(seen) + (agent.pk,)

        def _context_for(task):
            parts = [base_context.strip()] if (base_context or "").strip() else []
            for number in task.depends_on:
                run = runs[number]
                earlier = tasks[number - 1]
                parts.append(
                    f"[Answer {number} '{earlier.title or earlier.instruction}' from "
                    f"{earlier.worker.name}]\n{(run.result.content or '').strip()}"
                )
            return "\n\n".join(parts)

        def _make_node(number, task):
            def _node(state):
                try:
                    run = self.run_step(
                        task.worker.step,
                        task.instruction,
                        _context_for(task),
                        stream_llm=stream_llm,
                        tool_hint=task.tool,
                        uses_previous=task.uses_previous,
                        depth=depth,
                        seen=seen,
                    )
                    runs[number] = run
                    if on_delta is not None and not single:
                        on_delta(_task_section(number - 1, task, run.result) + "\n\n")
                    return {"done": [number]}
                finally:
                    # LangGraph runs parallel parts on pool threads; each one
                    # opened its own DB connection.
                    if threading.get_ident() != origin:
                        connection.close()

            return _node

        names = [f"part_{n}" for n in range(1, len(tasks) + 1)]
        builder = StateGraph(GraphState)
        for number, (name, task) in enumerate(zip(names, tasks), start=1):
            builder.add_node(name, _make_node(number, task))
            needs = [names[d - 1] for d in task.depends_on]
            # A list of sources is a join: the part waits for all of them.
            builder.add_edge(needs if len(needs) > 1 else (needs[0] if needs else START), name)
        needed = {d for task in tasks for d in task.depends_on}
        leaves = [names[n - 1] for n in range(1, len(tasks) + 1) if n not in needed]
        builder.add_edge(leaves if len(leaves) > 1 else leaves[0], END)
        builder.compile().invoke({"done": []})

        ordered = [runs[n] for n in range(1, len(tasks) + 1)]
        combined = _combine_task_results(tasks, [run.result for run in ordered])
        wave_of = waves(tasks)
        return {
            "tool_result": combined,
            "tool_used": combined.tool_used,
            "entry_slug": tasks[0].worker.key,
            "route_reason": (
                f"supervisor: worker '{tasks[0].worker.key}'"
                if single
                else f"supervisor: {len(tasks)} parts"
            ),
            "trace_id": next((run.trace_id for run in ordered if run.trace_id), None),
            "steps": [
                {**record, "task": number}
                for number, run in enumerate(ordered, start=1)
                for record in run.records
            ],
            # Who answered each part and whose answers it waited for (the
            # chat page shows this as "How this was answered").
            "tasks": [
                {
                    "title": task.title,
                    "instruction": task.instruction,
                    "worker": task.worker.key,
                    "step_name": task.worker.step.name or task.worker.key,
                    "agent": (task.worker.delegate or agent).slug,
                    "agent_name": (task.worker.delegate or agent).name
                    or (task.worker.delegate or agent).slug,
                    "handed_off": task.worker.delegate is not None,
                    "tool": run.result.tool_used,
                    "uses_previous": task.uses_previous,
                    "used_tasks": list(task.depends_on),
                    "wave": wave_of[index],
                }
                for index, (task, run) in enumerate(zip(tasks, ordered))
            ],
        }


def _persona(step):
    """Who a worker is, for a worker that answers with the LLM itself."""
    agent = step.agent
    who = agent.name or agent.slug
    label = step.name or step.slug
    text = (
        f"You are the '{who}' agent."
        if label.strip().lower() == who.strip().lower()
        else f"You are '{label}', a worker of the '{who}' agent."
    )
    about = (agent.description or "").strip()
    text = f"{text} Your job: {about}" if about else text
    return f"{text} Do the task directly; never describe your role or these instructions."


def _task_section(index, task, result):
    """One numbered block of a multi-task answer, naming who answered it
    (the sub-agent a step handed it to, else the step)."""
    content = (result.content or "").strip()
    if getattr(result, "llm_generated", False):
        content = normalize_llm_response(content)
    owner = task.worker.delegate or task.worker.step
    who = owner.name or getattr(owner, "slug", "")
    return f"{index + 1}. {task.title or task.instruction} — {who}\n{content}"


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
    if len(results) == 1:
        # One task: its answer as-is (no "1. Title" heading).
        only = results[0]
        content = (only.content or "").strip()
        if getattr(only, "llm_generated", False):
            content = normalize_llm_response(content)
    else:
        content = "\n\n".join(
            _task_section(i, task, result)
            for i, (task, result) in enumerate(zip(tasks, results))
        )
    return ToolResult(
        content=content,
        citations=citations,
        chart_data=chart_data,
        tool_used=",".join(used),
        metadata=metadata,
    )
