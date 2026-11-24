"""Run one chat turn through the Agent graph and save it.

``AgentChatService`` is what ``chat.services.router.service_for`` builds when the
send URL is an Agent's slug (``POST /chat/<name>/``).
The graph is the Agent's stored graph JSON (``Agent.graph``), loaded by
``chat.services.agent.graph``; Steps are never read here. Each (chat session,
agent) pair is one LangGraph thread (``thread_id`` = ``<session id>:<agent id>``)
kept by the Postgres checkpointer, so the agent remembers its own earlier turns,
tool calls included, but never sees another agent's tool calls.

Each saved turn records the checkpoint and thread it ended on
(``metadata.checkpoint_id`` / ``metadata.thread_id``) and the agent's next run
forks from it. A run that failed part-way still leaves its messages in the
thread; forking from the last saved turn means they are never reused. Turns
saved without this agent since then (Normal RAG, policy replies, other agents)
are replayed into the run's input as plain text.

``stream_message`` yields ``("trace", step)`` as the agent calls tools,
``("delta", text)`` for answer tokens, and finally ``("done", turn)``.
"""
from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.db import transaction
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from chat.services.agent.context import AgentContext
from chat.services.agent.tools import enabled_tool_rows
from chat.services import session_files
from chat.services.agent_capabilities import SUMMARY_RE
from chat.services.plain_text import normalize_llm_response
from chat.services.timing import timed, with_timing

logger = logging.getLogger(__name__)

_PICKER_ONLY_MESSAGE = "Process the selected files."
_PREVIEW_CHARS = 300
_ARGS_CHARS = 300


def _preview(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _attached_line(names) -> str:
    names = [n for n in names if n]
    return f"[Attached to this message: {', '.join(names)}]" if names else ""


def _turn_messages(turns, session):
    """Turns saved outside this agent's thread, as ONE context message.

    They were answered by Normal RAG, a policy reply or another agent, whose
    charts and tool calls are not in the text. Replayed as the model's own
    messages, the model copies them (replies that only describe a chart);
    as one quoted block it reads them as background. Each question notes the
    files attached to it.
    """
    lines = []
    for turn in turns:
        attached = _attached_line(
            chip.get("title") for chip in session_files.chips_for_media(session, turn.media_url)
        ) if turn.media_url else ""
        question = " ".join(p for p in (turn.question, attached) if p)
        if question:
            lines.append(f"User: {question}")
        if turn.response:
            lines.append(f"Assistant: {turn.response}")
    if not lines:
        return []
    return [
        HumanMessage(
            "Earlier messages in this chat, for context only. Other assistants "
            "answered them; their charts and tool calls are not shown here, so "
            "do not copy how they replied:\n\n" + "\n\n".join(lines)
        ),
        AIMessage("Understood."),
    ]

_CHART_BLOCK_RE = re.compile(r"```[ \t]*(?:chart|json)?[ \t]*\r?\n(.*?)```", re.DOTALL)


def chart_from_reply(text: str):
    """``(chart dict or None, text without the chart block)``.

    Reads the first fenced ```chart (or ```json) block that is a valid chart
    (``ChartData``). Other fenced blocks stay in the text.
    """
    from pydantic import ValidationError

    from chat.services.chat_service import ChartData

    for match in _CHART_BLOCK_RE.finditer(text or ""):
        try:
            chart = ChartData.model_validate(json.loads(match.group(1)))
        except (ValueError, TypeError, ValidationError):
            continue
        rest = (text[: match.start()] + text[match.end():]).strip()
        return chart.model_dump(mode="json"), re.sub(r"\n{3,}", "\n\n", rest)
    return None, text


class AgentChatService:
    def __init__(self, chat_service, langfuse, agent):
        # ``chat_service`` saves the turn (``create_turn``); the agent's own
        # model answers.
        self._chat = chat_service
        self._langfuse = langfuse
        self._agent = agent

    # ------------------------------------------------------------------ #
    # Public API (same shape as ChatService)
    # ------------------------------------------------------------------ #
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
        """Run the turn and return the saved ``Conversation`` turn."""
        for kind, value in self.stream_message(
            user,
            message_text,
            conversation=conversation,
            company=company,
            attachment_ids=attachment_ids,
            model_name=model_name,
            selected_entry_ids=selected_entry_ids,
            timing=timing,
        ):
            if kind == "done":
                return value
        return None

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
        """Generator of ``("trace", step)``, ``("delta", text)``, ``("done", turn)``.

        Raises only the types ``chat.views.send`` maps to HTTP statuses. A
        client that disconnects mid-stream does not lose the turn: the run is
        finished and saved without yielding more.
        """
        from chat.services import ChatProviderError

        self._check_turn(user, conversation, company)
        role, llm = self._runtime()
        ctx = AgentContext(
            user=user,
            company=company,
            session=conversation,
            agent=self._agent,
            llm=llm,
            role=role,
            files_note=session_files.files_overview(conversation, attachment_ids),
            attached_files=self._message_files(conversation, attachment_ids, message_text),
            message_text=message_text or "",
            attachment_ids=list(attachment_ids or []),
            selected_entry_ids=list(selected_entry_ids or []),
            retrieval_top_k=self._retrieval_top_k,
            tools=enabled_tool_rows(self._agent),
            timing=timing,
        )
        from chat.services.agent.graph import get_graph

        self._graph_json = self._agent.graph or {}
        graph = get_graph(self._graph_json)  # ValueError for an empty graph
        # One LangGraph thread per (chat, agent): an agent never sees another
        # agent's tool calls, so it cannot copy a tool it does not have.
        thread = {"configurable": {"thread_id": self._thread_id(conversation)}}
        with timed(timing, "history"):
            config, history = self._resume_point(graph, thread, conversation)
        text = message_text or (_PICKER_ONLY_MESSAGE if selected_entry_ids else "")
        # Name this message's files so "create a pie chart" is about them.
        attached = _attached_line(
            title for title, _ in ctx.attached_files
        ) if attachment_ids else ""
        text = "\n".join(p for p in (text, attached) if p)
        run_input = {"messages": history + [HumanMessage(text)]}

        trace, artifacts = [], []
        # Answer text seen / already streamed (a ```chart block is not streamed).
        self._answer_seen, self._answer_sent = "", 0
        disconnected = False
        try:
            with timed(timing, "llm"):
                for mode, chunk in graph.stream(
                    run_input,
                    config,
                    context=ctx,
                    stream_mode=["messages", "updates"],
                ):
                    for item in self._events(mode, chunk, trace, artifacts, timing):
                        if disconnected:
                            continue
                        try:
                            yield item
                        except GeneratorExit:
                            # Finish and save the turn; yield nothing more.
                            disconnected = True
                            logger.info(
                                "Agent chat stream closed by client; finishing the turn"
                            )
            turn = self._persist(
                graph,
                thread,
                ctx,
                user=user,
                message_text=message_text,
                attachment_ids=attachment_ids,
                trace=trace,
                artifacts=artifacts,
                timing=timing,
            )
        except Exception as exc:
            mapped = self._map_error(exc)
            if disconnected:
                logger.exception("Agent chat turn failed after the client disconnected")
                return
            if mapped is exc:
                raise
            raise mapped from exc
        if not disconnected:
            yield ("done", turn)

    # ------------------------------------------------------------------ #
    # Run setup
    # ------------------------------------------------------------------ #
    def _check_turn(self, user, conversation, company):
        if not user or not user.is_authenticated:
            raise ValueError("A signed-in user is required to chat.")
        if conversation is None:
            raise ValueError("An agent turn needs a chat session.")
        if (
            conversation.user_id != user.pk
            or conversation.company_id != getattr(company, "pk", None)
        ):
            raise ValueError("Conversation does not belong to the signed-in user.")
        # A global agent (no company) serves every company.
        if self._agent.company_id not in (None, getattr(company, "pk", None)):
            raise ValueError("Selected agent does not belong to the chat company.")

    def _runtime(self):
        """``(role, llm)`` from the agent's Langfuse prompt (one fetch per turn).

        The agent's config sets the model, temperature and max tokens, so it
        wins over the chat header's model picker.
        """
        from agents.services.agent_prompts import get_role_and_config
        from basics.llm import LLMClient

        role, config = get_role_and_config(self._agent, self._langfuse)
        self._retrieval_top_k = (config.get("retrieval") or {}).get("top_k")
        llm = LLMClient(
            provider=config["provider"],
            model=config["model"],
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
        )
        return role, llm

    def _thread_id(self, conversation) -> str:
        return f"{conversation.session_id}:{self._agent.pk}"

    def _resume_point(self, graph, thread, conversation):
        """``(config, history)``: where this run starts in the thread.

        After a saved agent turn: fork from its checkpoint and replay the turns
        saved since. With none: start the thread over, seeded with the recent
        turns.
        """
        config = {
            **thread,
            "recursion_limit": int(getattr(settings, "AGENT_RECURSION_LIMIT", 12)),
        }
        turns = conversation.turns.order_by("created_at")
        # The last turn THIS agent saved in this thread (other agents' turns
        # and Normal RAG turns are replayed below as plain text).
        last = (
            turns.filter(
                metadata__has_key="checkpoint_id",
                metadata__thread_id=thread["configurable"]["thread_id"],
            )
            .order_by("-created_at")
            .first()
        )
        if last is not None:
            config["configurable"] = {
                **thread["configurable"],
                "checkpoint_id": last.metadata["checkpoint_id"],
            }
            return config, _turn_messages(turns.filter(created_at__gt=last.created_at), conversation)
        # No saved agent turn: drop leftovers of failed runs, if any.
        graph.checkpointer.delete_thread(thread["configurable"]["thread_id"])
        recent = int(getattr(settings, "CHAT_RECENT_TURNS", 12))
        return config, _turn_messages(list(turns)[-recent:] if recent > 0 else [], conversation)

    # ------------------------------------------------------------------ #
    # Stream events
    # ------------------------------------------------------------------ #
    def _events(self, mode, chunk, trace, artifacts, timing):
        from chat.services.agent.graph import answer_node, model_nodes, node_step

        last_step = answer_node(self._graph_json)
        step_names = {n["id"]: n.get("name") or n["id"] for n in model_nodes(self._graph_json)}
        owner = node_step(self._graph_json)
        if mode == "messages":
            message, meta = chunk
            # Only the last step's reply is the answer; earlier steps' replies
            # appear in the trace.
            if (
                meta.get("langgraph_node") == last_step
                and isinstance(message, AIMessageChunk)
                and not message.tool_call_chunks
                and isinstance(message.content, str)
                and message.content
            ):
                if timing is not None:
                    timing.mark_first_token()
                visible = self._visible_delta(message.content)
                if visible:
                    yield ("delta", visible)
            return
        for node, update in (chunk or {}).items():
            step = owner.get(node)
            if step not in step_names:
                continue
            is_tools = node != step
            for message in (update or {}).get("messages", []):
                if not is_tools and isinstance(message, AIMessage):
                    for call in message.tool_calls or []:
                        item = {
                            "kind": "tool_call",
                            "step": step,
                            "tool": call["name"],
                            "args": _preview(
                                json.dumps(call.get("args") or {}, ensure_ascii=False),
                                _ARGS_CHARS,
                            ),
                        }
                        trace.append(item)
                        yield ("trace", item)
                    if not message.tool_calls and step != last_step:
                        item = {
                            "kind": "step_output",
                            "step": step,
                            "tool": step_names[step],
                            "preview": _preview(message.content, _PREVIEW_CHARS),
                        }
                        trace.append(item)
                        yield ("trace", item)
                elif is_tools and isinstance(message, ToolMessage):
                    if isinstance(message.artifact, dict):
                        artifacts.append(message.artifact)
                    item = {
                        "kind": "tool_result",
                        "step": step,
                        "tool": message.name,
                        "ok": message.status != "error",
                        "preview": _preview(message.content, _PREVIEW_CHARS),
                    }
                    trace.append(item)
                    yield ("trace", item)

    # ------------------------------------------------------------------ #
    # Save
    # ------------------------------------------------------------------ #
    def _persist(
        self, graph, thread, ctx, *, user, message_text, attachment_ids, trace, artifacts, timing
    ):
        from chat.services.agent.graph import model_nodes
        from chat.retitle import retitle_if_default
        from chat.services import ChatProviderError

        state = graph.get_state(thread)
        final = next(
            (m for m in reversed(state.values.get("messages", [])) if isinstance(m, AIMessage)),
            None,
        )
        # A chart written as a ```chart JSON block (no chart tool) is drawn
        # by the app like a create_chart result; the JSON leaves the text.
        reply_chart, raw = chart_from_reply(str(getattr(final, "content", "") or "").strip())
        content = normalize_llm_response(raw)
        if not content and reply_chart is not None:
            content = reply_chart["title"]
        if not content:
            raise ChatProviderError("The LLM provider returned an empty response.")

        citations, chart, tool_metadata, summary = [], reply_chart, {}, False
        for artifact in artifacts:
            # Keep only the passages the answer cites by their [n] number, so
            # a search the answer did not use adds no source chips.
            citations.extend(
                cite
                for n, cite in enumerate(artifact.get("citations") or [], start=1)
                if f"[{n}]" in content
            )
            chart = artifact.get("chart") or chart
            tool_metadata.update(artifact.get("metadata") or {})
            summary = summary or bool(artifact.get("summary"))
        tools_used = [s["tool"] for s in trace if s["kind"] == "tool_call"]

        with timed(None, "persist"), transaction.atomic():
            turn = self._chat.create_turn(
                ctx.session,
                user=user,
                question=message_text,
                response=content,
                model_name=ctx.llm.model,
                citations=citations,
                chart_data=chart,
                metadata=with_timing({
                    # Tool metadata (file_panel, temporal_links, ...) first so
                    # the fixed keys below always win.
                    **tool_metadata,
                    "source": "langgraph",
                    "tool_used": tools_used[-1] if tools_used else "",
                    "tools_used": tools_used,
                    "trace": trace,
                    "flow": [n["id"] for n in model_nodes(self._graph_json)],
                    "checkpoint_id": state.config["configurable"]["checkpoint_id"],
                    "thread_id": thread["configurable"]["thread_id"],
                    "agent_selection": {
                        "agent_id": str(self._agent.pk),
                        "agent_slug": self._agent.slug,
                        "outcome": "pipeline",
                    },
                }, timing),
                attachment_ids=attachment_ids,
                actor=user,
            )
            if summary and SUMMARY_RE.search(message_text or ""):
                # Only an asked-for summary replaces the session's summary
                # (the transcript tool also answers "what did I ask?").
                ctx.session.summary = content
                ctx.session.save(update_fields=["summary", "updated_at"])
            retitle_if_default(
                ctx.session, llm_title=None, fallback_text=message_text, actor=user
            )
        return turn

    @staticmethod
    def _message_files(conversation, attachment_ids, message_text):
        """``(title, text)`` of the files this message is about, so the model
        reads them without a tool (a CSV comes with exact value counts).

        The files attached to it; else, when it refers to a chat file ("this
        country file", "the education csv"), the file that matches its name
        ("this"/"that" with no matching name = the newest file), or the
        matching candidates. A name that matches no file gives nothing: the
        model has the file list and says the file is not there.
        """
        from chat.services import file_matching
        from chat.services.chat_service import ChatService

        ids = [str(i) for i in attachment_ids or []]
        if not ids:
            files = session_files.list_files(conversation)
            if not file_matching.references_file(message_text, files):
                return []
            target = file_matching.select_chart_file(files, message_text)
            if target.kind == "file":  # named, or "this file" = newest
                chosen = [target.file]
            elif target.kind == "ambiguous":
                chosen = list(target.candidates)[:3]
            else:  # names a file that is not here: the model says so and asks
                chosen = []
            ids = [f.id for f in chosen]
        if not ids:
            return []
        try:
            # question=None: read exactly these files (no re-matching).
            return ChatService()._chat_attachment_texts(
                conversation, attachment_ids=ids, question=None
            ) or []
        except Exception:
            logger.exception("Could not read the message's files for the agent")
            return []

    def _visible_delta(self, piece: str) -> str:
        """The part of ``piece`` to stream: everything before a ``` fence
        (a chart JSON block is drawn as a chart, never shown as text)."""
        self._answer_seen += piece
        fence = self._answer_seen.find("```")
        if fence != -1:
            visible_end = fence
        else:
            # Hold back trailing backticks that may start a fence.
            visible_end = len(self._answer_seen.rstrip("`"))
        if visible_end <= self._answer_sent:
            return ""
        out = self._answer_seen[self._answer_sent:visible_end]
        self._answer_sent = visible_end
        return out

    @staticmethod
    def _map_error(exc):
        """The exception ``send`` should see: known types pass, others become
        ``ChatProviderError`` (502)."""
        from basics.llm import LLMBackendError, LLMConfigurationError
        from chat.services import ChatProviderError, ChatRetrievalError

        if isinstance(
            exc,
            (LLMConfigurationError, LLMBackendError, ChatProviderError, ChatRetrievalError, ValueError),
        ):
            return exc
        logger.exception("Agent turn failed")
        return ChatProviderError("The LLM provider failed to generate a response.")
