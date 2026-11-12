"""Run one chat turn through the Agent graph and save it.

``AgentChatService`` is what ``chat.views.send`` builds for a selected Agent.
The graph is the Agent's stored graph JSON (``Agent.graph``), loaded by
``chat.agent.graph``; Steps are never read here. A chat session is a LangGraph thread (``thread_id`` = the session id) kept by
the Postgres checkpointer, so the agent remembers earlier turns, tool calls
included.

Each saved turn records the checkpoint it ended on (``metadata.checkpoint_id``)
and the next run forks from it. A run that failed part-way still leaves its
messages in the thread; forking from the last saved turn means they are never
reused. Turns saved without the agent since then (Normal RAG, policy replies)
are replayed into the run's input.

``stream_message`` yields ``("trace", step)`` as the agent calls tools,
``("delta", text)`` for answer tokens, and finally ``("done", turn)``.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings
from django.db import transaction
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from chat.agent.context import AgentContext
from chat.agent.tools import enabled_tool_rows
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


def _turn_messages(turns):
    """Saved turns as chat messages (an attachment-only turn has no question)."""
    messages = []
    for turn in turns:
        if turn.question:
            messages.append(HumanMessage(turn.question))
        if turn.response:
            messages.append(AIMessage(turn.response))
    return messages


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
            message_text=message_text or "",
            attachment_ids=list(attachment_ids or []),
            selected_entry_ids=list(selected_entry_ids or []),
            retrieval_top_k=self._retrieval_top_k,
            tools=enabled_tool_rows(self._agent),
            timing=timing,
        )
        from chat.agent.graph import get_graph

        self._graph_json = self._agent.graph or {}
        graph = get_graph(self._graph_json)  # ValueError for an empty graph
        thread = {"configurable": {"thread_id": str(conversation.session_id)}}
        with timed(timing, "history"):
            config, history = self._resume_point(graph, thread, conversation)
        text = message_text or (_PICKER_ONLY_MESSAGE if selected_entry_ids else "")
        run_input = {"messages": history + [HumanMessage(text)]}

        trace, artifacts = [], []
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
        last = (
            turns.filter(metadata__has_key="checkpoint_id")
            .order_by("-created_at")
            .first()
        )
        if last is not None:
            config["configurable"] = {
                **thread["configurable"],
                "checkpoint_id": last.metadata["checkpoint_id"],
            }
            return config, _turn_messages(turns.filter(created_at__gt=last.created_at))
        # No saved agent turn: drop leftovers of failed runs, if any.
        graph.checkpointer.delete_thread(thread["configurable"]["thread_id"])
        recent = int(getattr(settings, "CHAT_RECENT_TURNS", 12))
        return config, _turn_messages(list(turns)[-recent:] if recent > 0 else [])

    # ------------------------------------------------------------------ #
    # Stream events
    # ------------------------------------------------------------------ #
    def _events(self, mode, chunk, trace, artifacts, timing):
        from chat.agent.graph import answer_node, model_nodes, node_step

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
                yield ("delta", message.content)
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
        from chat.agent.graph import model_nodes
        from chat.retitle import retitle_if_default
        from chat.services import ChatProviderError

        state = graph.get_state(thread)
        final = next(
            (m for m in reversed(state.values.get("messages", [])) if isinstance(m, AIMessage)),
            None,
        )
        content = normalize_llm_response(str(getattr(final, "content", "") or "").strip())
        if not content:
            raise ChatProviderError("The LLM provider returned an empty response.")

        citations, chart, tool_metadata, summary = [], None, {}, False
        for artifact in artifacts:
            citations.extend(artifact.get("citations") or [])
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
