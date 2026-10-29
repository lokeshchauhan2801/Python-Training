"""LLM tool registry and handlers for the LangGraph chat runtime.

This module holds EVERYTHING tool-related so ``langgraph_service.py`` can stay
focused on the graph/routing implementation:

* ``ToolContext`` / ``ToolResult`` — the data passed to and returned from a tool.
* the registry (``register`` / ``get_handler`` / ``available_slugs``).
* every tool handler (``general_assistant``, ``document_search``,
  ``chat_summarise``, ``web_search``, ``prompt_tool``) plus the two
  chat-document tools (``add_to_knowledge_base``, ``document_processing_status``).

A tool is a function ``(ToolContext) -> ToolResult`` registered under a slug.
``langgraph_service`` imports the registry + context/result types from here and
re-exports them, so existing imports of
``chat.services.langgraph_service.available_slugs`` (etc.) keep working.

Charts: ``general_assistant`` and ``web_search`` are chart-capable. When the
user asks for a chart/graph (e.g. "pie chart of India's GDP growth last 5
years"), these tools return structured ``chart_data`` built from the model's
own knowledge; ``document_search`` already returns charts grounded in the
uploaded/attached file data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tool context / result
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
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
    pipeline_context: str = ""  # prior steps' accumulated output (multi-step pipeline)


@dataclass
class ToolResult:
    content: str
    citations: list = field(default_factory=list)
    chart_data: dict | None = None
    tool_used: str = ""
    ok: bool = True
    error: str | None = None
    metadata: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, Callable[["ToolContext"], "ToolResult"]] = {}


def register(slug: str):
    """Decorator registering ``handler`` under ``slug`` in the module registry."""

    def _decorator(handler: Callable[["ToolContext"], "ToolResult"]):
        _REGISTRY[slug] = handler
        return handler

    return _decorator


def get_handler(slug: str):
    """Return the handler registered under ``slug``, or ``None`` if unknown."""
    return _REGISTRY.get(slug)


def available_slugs() -> set[str]:
    """Return the set of slugs with a registered handler."""
    return set(_REGISTRY.keys())


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _history_messages(conversation, message_text):
    """Build provider-neutral chat messages from prior turns + this message."""
    messages = []
    for turn in conversation.turns.order_by("created_at"):
        messages.append({"role": "user", "content": turn.question})
        if turn.response:
            messages.append({"role": "assistant", "content": turn.response})
    messages.append({"role": "user", "content": message_text})
    return messages


_PROVIDER_DISPLAY = {"openai": "OpenAI", "deepseek": "DeepSeek"}


def identity_line(llm) -> str:
    """A system line stating the REAL provider/model serving this turn.

    Models are unreliable about their own identity (e.g. DeepSeek often claims
    to be OpenAI GPT because of its training data). We inject the ground-truth
    provider + model so 'which model are you using?' is answered correctly.
    """
    provider = getattr(llm, "provider", "") or ""
    model = getattr(llm, "model", "") or ""
    display = _PROVIDER_DISPLAY.get(provider, provider.capitalize() or "the configured provider")
    return (
        "Your identity: you are served by the "
        f"{display} API using the model '{model}'. If the user asks which model, "
        "provider, or LLM you are, answer with exactly this provider and model "
        f"name ({display}, model '{model}'). Do NOT claim to be OpenAI, ChatGPT, "
        "GPT-3.5/GPT-4, or any other provider/model unless that is the provider "
        "and model named here, even if your training data suggests otherwise."
    )


def _with_identity(system_text: str, llm) -> str:
    """Prepend the real-identity line to a system prompt (or start one)."""
    line = identity_line(llm)
    system_text = (system_text or "").strip()
    return f"{line}\n\n{system_text}" if system_text else line


def _with_pipeline_context(base_text: str, ctx: "ToolContext") -> str:
    """Prepend prior-step output to a prompt in the multi-step pipeline.

    Returns ``base_text`` UNCHANGED when ``ctx.pipeline_context`` is empty, so
    the single-step / RAG / existing-test paths stay byte-for-byte identical.
    When a prior step produced output, its accumulated text is prepended as a
    labelled block so this step's handler sees "step 1's output".
    """
    pc = (getattr(ctx, "pipeline_context", "") or "").strip()
    if not pc:
        return base_text
    block = f"[Pipeline context from previous steps]\n{pc}"
    base_text = (base_text or "").strip()
    return f"{block}\n\n{base_text}" if base_text else block


def _wants_chart(message_text: str) -> bool:
    """True when the user's message asks for a chart/graph/plot."""
    from chat.services.chat_service import ChatService

    return ChatService._is_chart_request(message_text or "")


def _knowledge_chart_reply(ctx: "ToolContext", tool_slug: str) -> "ToolResult":
    """Answer a chart request from the model's own (general/world) knowledge.

    Reuses ``ChatService._build_prompt`` with NO file context so the prompt's
    "general knowledge chart" branch is active (the model may fill the chart
    with its best real numeric values and mark it "(general knowledge)"). Used
    by ``general_assistant`` and ``web_search`` for requests like "pie chart of
    India's GDP growth last 5 years".
    """
    from chat.services.chat_service import ChatCompletion, ChatService

    # A chat file the request targets (D7) is charted from that file, even
    # when the router picked a general-knowledge tool (F.4).
    target = ChatService._chart_target(ctx.conversation, ctx.message_text)
    if target is not None and target.kind == "file":
        return _file_chart_reply(ctx, target, tool_slug=tool_slug)

    service = ChatService()
    prompt, _ = service._build_prompt(
        ctx.history_text,
        ctx.message_text,
        chunks=[],
        has_file_context=False,
        attachment_sections=[],
    )
    # Honour the tool's Langfuse system prompt (chart guidance lives there) by
    # prepending it, mirroring how document_search applies its own prompt.
    default_name = (
        "tool_web_search" if tool_slug == "web_search" else "tool_general_assistant"
    )
    pre = _with_identity(
        _with_pipeline_context(_system_prompt(ctx, default_name), ctx), ctx.llm
    )
    prompt = f"{pre.strip()}\n\n{prompt}"
    # No file is attached on this turn. If the user's message does not itself
    # name WHAT to chart (a bare "create a pie chart"), the model must ASK what
    # data to visualise instead of inventing a chart or reusing an earlier
    # turn's data (which produced the wrong "previous inflation chart" bug). A
    # self-contained request ("pie chart of India's GDP growth last 5 years")
    # is charted normally.
    prompt = (
        "Important: decide whether THIS user message specifies what data to "
        "chart. If it clearly names the subject/data (e.g. a country's GDP, a "
        "list of values), build the chart. If it is a bare request like "
        "'create a chart'/'make a pie chart' without naming any data, DO NOT "
        "invent a chart and DO NOT reuse data from earlier in the conversation; "
        "instead set chart to null and reply by asking the user what data they "
        "want to visualise (and that they can attach a file to chart its "
        "contents).\n\n"
    ) + prompt
    completion = ctx.llm.generate_structured(prompt, ChatCompletion)
    chart_data = completion.chart.model_dump(mode="json") if completion.chart else None
    content = ChatService._strip_chart_json_blocks(completion.response).strip()
    # When a chart is produced, keep only a short caption — the data lives in
    # the chart, so don't also dump every value as text (unless the user asked).
    if chart_data is not None:
        content = _chart_caption_only(content, ctx.message_text)
    if not content:
        content = (
            "Here is the chart you requested."
            if chart_data is not None
            else "I could not produce a chart for that request."
        )
    return ToolResult(content=content, chart_data=chart_data, tool_used=tool_slug)


def _file_chart_reply(
    ctx: "ToolContext", target, tool_slug: str = "document_search"
) -> "ToolResult":
    """Chart exactly the D7 ``target`` chat file (F.4, R7).

    A CSV gets the deterministic CSV chart. Otherwise the LLM charts only that
    file's text (no library chunks, ``has_file_context=True``). A file with no
    readable data gets a fixed reply and no LLM call. An LLM chart names its
    source file (H.3).
    """
    from chat.services.chat_service import ChatCompletion, ChatService

    service = ChatService()
    title = target.file.title
    local = service._build_csv_distribution_chart(
        ctx.conversation, ctx.message_text
    )
    if local is not None:
        response_text, chart = local
        return ToolResult(
            content=response_text,
            chart_data=chart.model_dump(mode="json"),
            tool_used=tool_slug,
        )

    sections = service._chat_attachment_texts(
        ctx.conversation, question=ctx.message_text
    )
    if not sections:
        return ToolResult(
            content=ChatService._unreadable_chart_reply(title), tool_used=tool_slug
        )
    prompt, _ = service._build_prompt(
        ctx.history_text,
        ctx.message_text,
        [],
        has_file_context=True,
        attachment_sections=sections,
    )
    pre = _with_identity(
        _with_pipeline_context(_system_prompt(ctx, "tool_document_search"), ctx),
        ctx.llm,
    )
    prompt = f"{pre.strip()}\n\n{prompt}"
    completion = ctx.llm.generate_structured(prompt, ChatCompletion)
    chart_data = completion.chart.model_dump(mode="json") if completion.chart else None
    content = ChatService._strip_chart_json_blocks(completion.response).strip()
    if chart_data is not None:
        content = _chart_caption_only(content, ctx.message_text)
    if not content:
        content = (
            "Here is the chart you requested."
            if chart_data is not None
            else "I could not produce a chart for that request."
        )
    if chart_data is not None:
        content = ChatService._with_source_line(content, title)
    return ToolResult(content=content, chart_data=chart_data, tool_used=tool_slug)


def _system_prompt(ctx: "ToolContext", default_name: str, fallback: str = "") -> str:
    """Resolve a Langfuse system prompt for the tool (or a fallback string)."""
    name = (ctx.tool.prompt_name if ctx.tool else "") or default_name
    return ctx.langfuse.get_prompt(name, fallback=fallback) if ctx.langfuse else fallback


def _chart_caption_only(content: str, message_text: str) -> str:
    """Trim a chart reply's prose to a short caption (delegates to ChatService).

    When a chart is returned, the chart carries the data, so the text should be
    a brief caption — not a re-listing of every value. Skipped when the user
    explicitly asked for the numbers/data/table too.
    """
    from chat.services.chat_service import ChatService

    return ChatService._chart_caption_only(content, message_text)


# --------------------------------------------------------------------------- #
# General / world-knowledge tools
# --------------------------------------------------------------------------- #
@register("general_assistant")
def _general_assistant(ctx: "ToolContext") -> "ToolResult":
    """``general_assistant`` tool — a plain LLM answer, no retrieval.

    Chart-capable: when the message asks for a chart, answers with a structured
    chart built from the model's own knowledge instead of plain text.
    """
    if _wants_chart(ctx.message_text):
        return _knowledge_chart_reply(ctx, "general_assistant")

    messages = _history_messages(ctx.conversation, ctx.message_text)
    sys = _with_identity(
        _with_pipeline_context(_system_prompt(ctx, "tool_general_assistant"), ctx),
        ctx.llm,
    )
    messages.insert(0, {"role": "system", "content": sys})

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="general_assistant")


@register("web_search")
def _web_search(ctx: "ToolContext") -> "ToolResult":
    """``web_search`` tool.

    No external search-engine provider is wired in, so rather than refusing with
    a dead "not configured" message (which left "take from internet" questions
    stuck), this answers from the model's own general knowledge. The model is
    told it has no live web access and should answer from what it knows, adding a
    brief caveat that the information may be out of date and giving its best
    current answer instead of declining.

    Chart-capable: a chart request (e.g. "pie chart of India's GDP growth last 5
    years") returns a structured chart built from the model's knowledge.
    """
    if _wants_chart(ctx.message_text):
        return _knowledge_chart_reply(ctx, "web_search")

    messages = _history_messages(ctx.conversation, ctx.message_text)
    sys = _system_prompt(ctx, "tool_web_search")
    if not sys.strip():
        sys = (
            "You are a helpful assistant. You do not have live internet access "
            "right now, so answer the user's question from your own general "
            "knowledge rather than declining. Give your best, most accurate "
            "answer, and add a one-line note that you could not fetch live web "
            "results and the information may be out of date."
        )
    messages.insert(
        0,
        {
            "role": "system",
            "content": _with_identity(_with_pipeline_context(sys, ctx), ctx.llm),
        },
    )

    content = ctx.llm.complete(messages)
    return ToolResult(content=content, tool_used="web_search")


# --------------------------------------------------------------------------- #
# Document / RAG tools
# --------------------------------------------------------------------------- #
@register("document_search")
def _document_search(ctx: "ToolContext") -> "ToolResult":
    """``document_search`` tool — the RAG/document path reused as a tool.

    Already chart-capable: charts are built from the retrieved/attached file
    data (``ChatCompletion.chart``) when the user asks for one.
    """
    from chat.services.chat_service import ChatCompletion, ChatService

    service = ChatService()
    # The current turn's attachments. In the LangGraph path the user message is
    # not linked yet, so pass these explicitly — otherwise attachment resolution
    # grounds on an EARLIER file (the "attached CSV returned the previous
    # letter's answer" bug).
    attachment_ids = list(ctx.attachment_ids or [])

    # Deterministic CSV chart: an attached CSV + a chart request is charted from
    # the file's real values by the local builder, not the LLM. This is the
    # reliable path for "create a pie chart" on a CSV like county_uk.csv.
    if service._is_chart_request(ctx.message_text):
        # A chart request with a D7 file target charts only that file.
        target = ChatService._chart_target(ctx.conversation, ctx.message_text)
        if target is not None and target.kind == "file":
            return _file_chart_reply(ctx, target)
        # With a file attached this turn, ground on exactly that file
        # (current-turn-wins). Without one, pass no attachment_ids so the
        # session-scoped CSV fallback in _chat_csv_documents reaches an earlier
        # session CSV — the follow-up "create a bar chart" case.
        local = service._build_csv_distribution_chart(
            ctx.conversation,
            ctx.message_text,
            attachment_ids=attachment_ids or None,
        )
        if local is not None:
            response_text, chart = local
            return ToolResult(
                content=response_text,
                chart_data=chart.model_dump(mode="json"),
                tool_used="document_search",
            )

    chunks = service._find_relevant_chunks(
        ctx.user,
        ctx.message_text,
        conversation=ctx.conversation,
        company=ctx.company,
    )
    # Chat attachments are NOT embedded; their text is read directly and folded
    # into the prompt. Without this the document_search tool answered "no
    # content" for an attached file even though its text was available.
    attachment_sections = service._chat_attachment_texts(
        ctx.conversation,
        attachment_ids=attachment_ids or None,
        question=ctx.message_text,
    )
    prompt, citations = service._build_prompt(
        ctx.history_text,
        ctx.message_text,
        chunks,
        attachment_sections=attachment_sections,
    )

    pre = _with_identity(
        _with_pipeline_context(_system_prompt(ctx, "tool_document_search"), ctx),
        ctx.llm,
    )
    prompt = f"{pre.strip()}\n\n{prompt}"
    completion = ctx.llm.generate_structured(prompt, ChatCompletion)
    chart_data = completion.chart.model_dump(mode="json") if completion.chart else None
    content = completion.response.strip()
    # With a chart, keep the text to a short caption (data is in the chart)
    # unless the user explicitly asked for the numbers/table too.
    if chart_data is not None:
        content = _chart_caption_only(content, ctx.message_text)
    return ToolResult(
        content=content,
        citations=citations,
        chart_data=chart_data,
        tool_used="document_search",
    )


@register("chat_summarise")
def _chat_summarise(ctx: "ToolContext") -> "ToolResult":
    """``chat_summarise`` tool — summarise the conversation's prior history."""
    from chat.services.chat_service import ChatService, _SUMMARY_SYSTEM_PROMPT

    service = ChatService()
    turns = list(ctx.conversation.turns.order_by("created_at"))
    conversation_context = service._build_conversation_context(turns)
    # In a multi-step pipeline the summary should also cover prior-step output.
    conversation_context = _with_pipeline_context(conversation_context, ctx)

    name = (ctx.tool.prompt_name if ctx.tool else "") or "tool_chat_summarise"
    sys = (
        ctx.langfuse.get_prompt(name, fallback=_SUMMARY_SYSTEM_PROMPT)
        if ctx.langfuse
        else _SUMMARY_SYSTEM_PROMPT
    )

    try:
        if "{{conversation}}" in sys or "{{history}}" in sys:
            system_content = sys.replace(
                "{{conversation}}", conversation_context
            ).replace("{{history}}", conversation_context)
            messages = [{"role": "system", "content": system_content}]
        else:
            messages = [
                {"role": "system", "content": sys},
                {"role": "user", "content": conversation_context},
            ]
    except Exception:
        messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": conversation_context},
        ]

    content = ctx.llm.complete(messages)

    # Persist the summary onto the session so it is saved with the chat and
    # OVERWRITTEN on each subsequent summary (the session keeps the latest).
    try:
        session = ctx.conversation
        if session is not None and content and content.strip():
            session.summary = content.strip()
            session.save(update_fields=["summary", "updated_at"])
    except Exception:  # noqa: BLE001 - persistence must never break the reply
        logger.exception("Could not persist chat summary to session")

    return ToolResult(content=content, tool_used="chat_summarise")


@register("prompt_tool")
def _prompt_tool(ctx: "ToolContext") -> "ToolResult":
    """``prompt_tool`` — generic prompt-only tool driven by a Langfuse prompt."""
    messages = _history_messages(ctx.conversation, ctx.message_text)

    name = (ctx.tool.prompt_name if ctx.tool else "") or ""
    sys = ctx.langfuse.get_prompt(name, fallback="") if (ctx.langfuse and name) else ""
    messages.insert(
        0,
        {
            "role": "system",
            "content": _with_identity(_with_pipeline_context(sys, ctx), ctx.llm),
        },
    )

    content = ctx.llm.complete(messages)

    used = ctx.tool.slug if ctx.tool and ctx.tool.slug else "prompt_tool"
    return ToolResult(content=content, tool_used=used)


# --------------------------------------------------------------------------- #
# Chat-document tools: add-to-knowledge-base (Temporal) + processing status
# --------------------------------------------------------------------------- #
@register("add_to_knowledge_base")
def _add_to_knowledge_base(ctx: "ToolContext") -> "ToolResult":
    """``add_to_knowledge_base`` tool — process / stop / resume / list the
    chat's files through the same command service as ``views.send`` (F.4).

    A gated-out STOP/RESUME (or any other kind) falls back to PROCESS, because
    the router already chose a KB tool.
    """
    from chat.services.file_commands import Kind

    return _file_command_tool(
        ctx,
        allowed={Kind.PROCESS, Kind.STOP, Kind.RESUME, Kind.LIST},
        default=Kind.PROCESS,
        tool_used="add_to_knowledge_base",
    )


@register("document_processing_status")
def _document_processing_status(ctx: "ToolContext") -> "ToolResult":
    """``document_processing_status`` tool — the chat files' processing status.

    Answers through the same command service as ``views.send`` (F.4); STOP,
    RESUME and LIST pass through, anything else reports STATUS.
    """
    from chat.services.file_commands import Kind

    return _file_command_tool(
        ctx,
        allowed={Kind.STOP, Kind.RESUME, Kind.STATUS, Kind.LIST},
        default=Kind.STATUS,
        tool_used="document_processing_status",
    )


def _file_command_tool(ctx, *, allowed, default, tool_used) -> "ToolResult":
    from chat.services import file_commands

    kind = file_commands.effective_kind(
        file_commands.classify(ctx.message_text), ctx.conversation
    )
    if kind not in allowed:
        kind = default
    reply = file_commands.handle_file_command(
        session=ctx.conversation,
        kind=kind,
        message=ctx.message_text,
        attachment_ids=list(ctx.attachment_ids or []),
        entry_ids=None,
        actor=ctx.user,
    )
    return ToolResult(
        content=reply.content, metadata=dict(reply.metadata), tool_used=tool_used
    )
