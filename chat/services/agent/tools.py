"""The Agent's tools: LangChain tools the model calls through ``ToolNode``.

Each built-in tool is a ``@tool(response_format="content_and_artifact")``
function. The model reads the tool's description (the agent's ``Tool`` row) and
arguments schema, calls it, and gets the text back as a ``ToolMessage``; the
artifact (citations, a chart, a file panel) never reaches the model and is
collected by ``AgentChatService`` for the saved turn. Tools return data; the
agent model writes the answer.

``ToolRuntime`` gives a tool the run's ``AgentContext`` (user, session,
attachments, ...). A ``Tool`` row whose slug is not built in is a prompt-only
tool: a one-call sub-assistant driven by the row's description.
"""
from __future__ import annotations

import logging
import warnings
from typing import Literal

from langchain_core.tools import StructuredTool, tool
from langgraph.prebuilt import ToolRuntime

from chat.services.chat_service import ChartData, ChartPoint, ChatService

logger = logging.getLogger(__name__)

# Passing the run context through ToolRuntime makes pydantic warn about
# serialising it; the value is never serialised.
warnings.filterwarnings(
    "ignore", message="Pydantic serializer warnings", category=UserWarning
)

NOT_ENABLED = "This tool is not enabled for this agent."


def _context(runtime: ToolRuntime):
    return runtime.context


def _enabled(runtime: ToolRuntime, slug: str) -> bool:
    return slug in (_context(runtime).tools or {})


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@tool(response_format="content_and_artifact")
def document_search(query: str, runtime: ToolRuntime) -> tuple[str, dict]:
    """Search the company's knowledge-base documents and the files in this chat.

    Args:
        query: What to look for, in the user's words (include file names the
            user mentioned).
    """
    if not _enabled(runtime, "document_search"):
        return NOT_ENABLED, {}
    from pathlib import Path

    from chat.services.kb import knowledge_base_overview

    ctx = _context(runtime)
    service = ChatService(retrieval_top_k=ctx.retrieval_top_k)
    # The user's message decides which chat files are referenced; the query
    # drives retrieval.
    question = f"{ctx.message_text}\n{query}".strip()
    chunks, sections = service._prepare_context(
        ctx.user,
        question,
        conversation=ctx.session,
        company=ctx.company,
        attachment_ids=list(ctx.attachment_ids) or None,
        skip_chunks=False,
        timing=ctx.timing,
    )
    sections = list(sections or []) + service.csv_sections(ctx.session, question)

    blocks, citations = [], []
    for title, text in sections:
        if text:
            blocks.append(f"[Chat file: {title}]\n{text}")
    for n, chunk in enumerate(chunks, start=1):
        filename = (
            Path(chunk.document.file.name).name
            if chunk.document.file
            else chunk.document.title
        )
        location = chunk.source_location or f"page {chunk.page_number}"
        blocks.append(f"[{n}] {filename} ({location})\n{chunk.content}")
        citations.append(
            {
                "reference_id": str(chunk.pk),
                "document_id": str(chunk.document_id),
                "filename": filename,
                "page": chunk.page_number,
                "source_location": chunk.source_location,
                "excerpt": chunk.content[:300],
            }
        )
    overview = knowledge_base_overview(company=ctx.company)
    if overview:
        blocks.append(f"[Knowledge base documents]\n{overview}")
    if not blocks:
        return "No relevant passages were found in the documents or chat files.", {}
    return "\n\n".join(blocks), {"citations": citations}


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
@tool(response_format="content_and_artifact")
def create_chart(
    type: Literal["pie", "bar", "line"],
    title: str,
    x_axis: str,
    y_axis: str,
    series: list[str],
    data: list[ChartPoint],
    runtime: ToolRuntime,
) -> tuple[str, dict]:
    """Show a pie, bar or line chart in the chat.

    Args:
        type: Chart type. A pie chart has exactly one series.
        title: Chart title.
        x_axis: X axis label (the category name).
        y_axis: Y axis label (what the values measure).
        series: Series names, one per value in each data point (max 6).
        data: Points: a label and one number per series (max 100 points).
    """
    if not _enabled(runtime, "create_chart"):
        return NOT_ENABLED, {}
    chart = ChartData(
        type=type, title=title, x_axis=x_axis, y_axis=y_axis, series=series, data=data
    )
    return (
        f"The {chart.type} chart '{chart.title}' is shown to the user. Reply with "
        "a short caption only.",
        {"chart": chart.model_dump(mode="json")},
    )


@tool(response_format="content_and_artifact")
def chart_from_file(
    file_name: str,
    runtime: ToolRuntime,
    chart_type: Literal["pie", "bar", "line"] = "bar",
) -> tuple[str, dict]:
    """Chart a CSV file from this chat or the knowledge base by counting its values.

    Args:
        file_name: The CSV file or dataset name the user mentioned.
        chart_type: pie, bar or line.
    """
    if not _enabled(runtime, "chart_from_file"):
        return NOT_ENABLED, {}
    ctx = _context(runtime)
    local = ChatService()._build_csv_distribution_chart(
        ctx.session,
        f"{chart_type} chart of {file_name}",
        attachment_ids=list(ctx.attachment_ids) or None,
    )
    if local is None:
        return f"No CSV file matching '{file_name}' was found to chart.", {}
    text, chart = local
    return (
        f"{text}\nThe chart is shown to the user. Reply with a short caption only.",
        {"chart": chart.model_dump(mode="json")},
    )


# --------------------------------------------------------------------------- #
# Conversation and web
# --------------------------------------------------------------------------- #
@tool(response_format="content_and_artifact")
def chat_summarise(runtime: ToolRuntime) -> tuple[str, dict]:
    """Return this conversation's transcript so you can summarise it."""
    if not _enabled(runtime, "chat_summarise"):
        return NOT_ENABLED, {}
    ctx = _context(runtime)
    service = ChatService()
    turns = list(ctx.session.turns.order_by("created_at"))
    transcript = service._build_conversation_context(
        turns, recent_limit=ChatService._SUMMARY_RECENT_TURNS
    )
    # The answer that follows is the summary: saved on the session afterwards.
    return transcript or "(this conversation has no earlier messages)", {"summary": True}


@tool(response_format="content_and_artifact")
def web_search(query: str, runtime: ToolRuntime) -> tuple[str, dict]:
    """Look up web, online, current or external information.

    Args:
        query: What to look up.
    """
    if not _enabled(runtime, "web_search"):
        return NOT_ENABLED, {}
    return (
        "No live web results are available. Continue the task with your own "
        "general knowledge for this query: give your best real figures (and "
        "chart them with a chart tool if the user asked for a chart, adding "
        "\"(general knowledge)\" to the title). Do not ask the user for "
        "permission; mention once, briefly, that the figures are approximate "
        "and may be out of date.",
        {},
    )


# --------------------------------------------------------------------------- #
# Chat files: knowledge-base processing (Temporal) and status
# --------------------------------------------------------------------------- #
def _file_command(runtime, slug, action, file_names, destination=None):
    from chat.services import file_commands

    if not _enabled(runtime, slug):
        return NOT_ENABLED, {}
    ctx = _context(runtime)
    # The picker selection belongs to process / resume / status, as in chat.
    entry_ids = (
        list(ctx.selected_entry_ids)
        if action in ("process", "resume", "status") and ctx.selected_entry_ids
        else None
    )
    reply = file_commands.handle_file_command(
        session=ctx.session,
        kind=action,
        message=(file_names or "").strip() or ctx.message_text,
        attachment_ids=list(ctx.attachment_ids),
        entry_ids=entry_ids,
        actor=ctx.user,
        destination=destination,
    )
    return reply.content, {"metadata": dict(reply.metadata)}


@tool(response_format="content_and_artifact")
def add_to_knowledge_base(
    runtime: ToolRuntime,
    destination: Literal["chat", "knowledge_base"],
    action: Literal["process", "stop", "resume", "cancel", "list"] = "process",
    file_names: str = "",
) -> tuple[str, dict]:
    """Process this chat's files through Temporal, or stop / resume / cancel that processing.

    "Process this file" means destination "chat": the file is processed and
    its text stays in this chat only (no Documents entry, no knowledge base).
    Use "knowledge_base" only when the user explicitly asks to add the file to
    the company knowledge base or company documents. When the user's wording
    does not say which (for example "add this file" or "upload it"), ask them
    before calling this tool. The reply names each file's workflow id.

    Args:
        destination: chat (process only in this chat) or knowledge_base.
        action: process, stop, resume, cancel, or list the files.
        file_names: The file names the user mentioned; empty for this turn's
            attachments or the files the user picked.
    """
    from chat.services import file_commands

    dest = "kb" if destination == "knowledge_base" else "chat"
    if action == "process" and dest == "kb":
        ctx = _context(runtime)
        if not file_commands.kb_requested(ctx.session, ctx.message_text):
            # The user did not ask for the knowledge base: never add it there
            # on the model's guess. Plain "process" stays in the chat; unclear
            # wording gets the question.
            said = file_commands.process_destination(ctx.message_text)
            dest = "chat" if said == "chat" else "ask"
    return _file_command(runtime, "add_to_knowledge_base", action, file_names, dest)


@tool(response_format="content_and_artifact")
def document_processing_status(
    runtime: ToolRuntime,
    action: Literal["status", "stop", "resume", "cancel", "list"] = "status",
    file_names: str = "",
) -> tuple[str, dict]:
    """Report or manage the processing of this chat's files and documents.

    Args:
        action: status, stop, resume, cancel, or list the files.
        file_names: The file names the user mentioned; empty for all files.
    """
    return _file_command(runtime, "document_processing_status", action, file_names)


BUILTIN_TOOLS = {
    t.name: t
    for t in (
        document_search,
        create_chart,
        chart_from_file,
        chat_summarise,
        web_search,
        add_to_knowledge_base,
        document_processing_status,
    )
}


# A step with this tool answers from the model's own knowledge: it binds no
# LangChain tool (the model always answers directly when no tool is needed).
ANSWER_DIRECTLY = "general_assistant"


def available_slugs() -> set[str]:
    """Slugs backed by built-in code (any other Tool slug is prompt-only)."""
    return set(BUILTIN_TOOLS) | {ANSWER_DIRECTLY}


# --------------------------------------------------------------------------- #
# Prompt-only tools and the per-run tool list
# --------------------------------------------------------------------------- #
def _prompt_tool(row) -> StructuredTool:
    """A Tool row with no built-in: one LLM call that runs on its description."""
    slug = row.slug

    def run(request: str, runtime: ToolRuntime) -> tuple[str, dict]:
        from chat.services.agent.prompts import PLAIN_TEXT_GUIDANCE

        ctx = _context(runtime)
        if slug not in (ctx.tools or {}):
            return NOT_ENABLED, {}
        system = f"{(row.description or '').strip()}\n\n{PLAIN_TEXT_GUIDANCE}".strip()
        content = ctx.llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": request}]
        )
        return content, {}

    return StructuredTool.from_function(
        run,
        name=slug,
        description=(row.description or row.name or slug).strip(),
        response_format="content_and_artifact",
        args_schema=None,
    )


def _described(base, row):
    """A built-in tool whose description is the agent's Tool row docs."""
    description = (row.description or "").strip()
    if not description or description == base.description:
        return base
    return base.model_copy(update={"description": description})


def tools_for(ctx, slugs=None) -> list:
    """The LangChain tools the model may call: ``slugs`` (one step's tools)
    or, by default, every tool in ``ctx.tools``."""
    rows = ctx.tools or {}
    tools = []
    for slug in (rows if slugs is None else slugs):
        row = rows.get(slug)
        if row is None:
            continue
        base = BUILTIN_TOOLS.get(slug)
        tools.append(_described(base, row) if base is not None else _prompt_tool(row))
    return tools


def enabled_tool_rows(agent) -> dict:
    """``{slug: Tool}`` for the tools the agent's graph JSON binds.

    Read from ``agent.graph`` (never from Steps); rows are the agent's scope
    (a global agent uses the global tools) and must still be enabled.
    """
    from agents.models import Tool
    from chat.services.agent.graph import tool_slugs

    slugs = tool_slugs(agent.graph)
    if not slugs:
        return {}
    rows = Tool.objects.filter(
        company_id=agent.company_id, slug__in=slugs, is_enabled=True
    ).order_by("slug")
    return {row.slug: row for row in rows}
