from __future__ import annotations

import csv
import json
import logging
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import StringIO, TextIOWrapper
from pathlib import Path
from typing import Literal

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from pydantic import BaseModel as PydanticBaseModel, Field, model_validator

from basics.llm import LLMClient
from documents import services as document_services
from documents.models import Document, DocumentChunk

from chat.models import Conversation, ConversationSession
from chat.retitle import retitle_if_default
from chat.services import file_matching, session_files
from chat.services.plain_text import normalize_llm_response
from chat.services.session_files import FileUnavailable
from chat.services.timing import timed, with_timing

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful assistant. Summarize the following conversation "
    "concisely, capturing the key questions and conclusions. Format the "
    "visible response as plain text with normal sentences, dot bullets, or "
    "numbered lists. Do not use Markdown emphasis or asterisk bullets."
)

_DEFAULT_MODEL_NAME = "gpt-4o-mini"


class ChatProviderError(Exception):
    """Raised when the configured LLM provider cannot generate a response."""


class ChatRetrievalError(Exception):
    """Raised when document references cannot be retrieved from vector storage."""


class ChartPoint(PydanticBaseModel):
    label: str = Field(min_length=1, max_length=100)
    values: list[float] = Field(min_length=1, max_length=6)


class ChartData(PydanticBaseModel):
    type: Literal["pie", "bar", "line"]
    title: str = Field(min_length=1, max_length=200)
    x_axis: str = Field(min_length=1, max_length=100)
    y_axis: str = Field(min_length=1, max_length=100)
    series: list[str] = Field(min_length=1, max_length=6)
    data: list[ChartPoint] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_series_data(self):
        if self.type == "pie" and len(self.series) != 1:
            raise ValueError("Pie charts must have exactly one data series.")
        if len(set(self.series)) != len(self.series):
            raise ValueError("Chart series names must be unique.")
        for point in self.data:
            if len(point.values) != len(self.series):
                raise ValueError("Each chart point must have one value per series.")
            if any(not math.isfinite(value) for value in point.values):
                raise ValueError("Chart values must be finite numbers.")
            if self.type == "pie" and any(value < 0 for value in point.values):
                raise ValueError("Pie chart values cannot be negative.")
        if self.type == "pie" and not any(
            point.values[0] > 0 for point in self.data
        ):
            raise ValueError("Pie charts must contain a positive value.")
        return self


class ChatCompletion(PydanticBaseModel):
    response: str = Field(min_length=1)
    chart: ChartData | None = None
    title: str | None = Field(default=None, max_length=200)


@dataclass(frozen=True)
class _PreparedTurn:
    """What ``ChatService._prepare_turn`` hands to the LLM and finalize steps."""

    user: object
    message_text: str
    conversation: object
    company: object
    attachment_ids: object
    assistant_model_name: str
    chart_file: object
    structured: bool
    chunks: list
    attachment_sections: list
    prompt: str
    citations: list


class _LibraryCsv:
    """A knowledge-base CSV ``Document`` shaped like a chat ``SessionFile``
    (``id`` / ``title`` / ``open()``) for ``_build_csv_distribution_chart``."""

    def __init__(self, document):
        self._document = document
        self.id = str(document.pk)
        self.title = document.title

    def open(self):
        try:
            return self._document.file.open("rb")
        except OSError as exc:
            raise FileUnavailable(self.id) from exc


class ChatService:
    def __init__(self, llm_client: LLMClient | None = None):

        self._llm = llm_client or LLMClient()


    def _session_messages(self, session):
        """Build provider-neutral messages from the session's prior turns."""
        messages = []
        for turn in session.turns.order_by("created_at"):
            messages.append({"role": "user", "content": turn.question})
            if turn.response:
                messages.append({"role": "assistant", "content": turn.response})
        return messages

    def ask(self, *, session, user, question, actor=None, **context) -> Conversation:
        """Ask a question in a session; persist and return the created turn."""
        messages = self._session_messages(session)
        messages.append({"role": "user", "content": question})

        answer = normalize_llm_response(self._llm.complete(messages))

        turn = Conversation(
            session=session,
            user=user,
            company=session.company,
            model_name=self._llm.model,
            question=question,
            response=answer,
            context=context or {},
            metadata={"provider": self._llm.provider, "model": self._llm.model},
        )


        turn.full_clean()
        turn.save(actor=actor or user)
        return turn

    def summarize(self, *, session, actor=None) -> str:
        """Summarize a session; persist the summary onto the session."""
        messages = [{"role": "system", "content": _SUMMARY_SYSTEM_PROMPT}]
        messages.extend(self._session_messages(session))

        summary = normalize_llm_response(self._llm.complete(messages))

        session.summary = summary
        session.save(
            actor=actor,
            update_fields=["summary", "updated_at", "updated_by"],
        )
        return summary


    def create_turn(
        self,
        session,
        *,
        user,
        question,
        response,
        model_name=None,
        citations=None,
        chart_data=None,
        context=None,
        metadata=None,
        attachment_ids=None,
        actor=None,
    ):
        """Persist ONE ``Conversation`` turn on ``session`` and return it.

        A turn collapses a user question and the assistant response into a
        single row (the two-table history model). The turn always inherits
        ``session.company`` (the company-match invariant is enforced in
        ``Conversation.save()``). The save is actor-aware so the audit columns
        are stamped from the acting user. Attachments (session file entry ids,
        when given) are recorded as chips in ``context["attachments"]`` as
        ``[{"id", "title"}]``; ids not in the session's file lists are skipped.

        Chart/citations render-state no longer has its own columns: ``citations``
        and ``chart_data`` are folded into the turn's ``context`` under the
        reserved keys ``"citations"`` and ``"chart"`` (merged into any
        caller-supplied ``context``, never clobbering it). ``chart_data`` is
        expected to be an ALREADY-validated ChartData dict (callers validate
        upstream via the Pydantic ``ChartData`` model); this method only stores
        it.
        """
        turn_context = dict(context or {})
        # ``citations`` is always present (defaults to []) so the read path /
        # serializer can rely on ``context.get("citations", [])``.
        turn_context["citations"] = citations or []
        # NOTE: ``context["chart"]`` is RENDER-STATE for the frontend, NOT prompt
        # text. The key is written only when a chart exists and must never be
        # serialized into an LLM prompt (prompt building reads only
        # question/response text, never a turn's raw context dict).
        if chart_data is not None:
            turn_context["chart"] = chart_data
        # Render-state too: the user bubble's file chips. Never prompt text.
        chips = session_files.attachment_chips(session, attachment_ids)
        if chips:
            turn_context["attachments"] = chips
        turn = Conversation(
            company=session.company,
            session=session,
            user=user,
            model_name=model_name or _DEFAULT_MODEL_NAME,
            question=question,
            response=response,
            context=turn_context,
            metadata=metadata or {},
        )
        turn.save(actor=actor or user)
        return turn

    def _find_relevant_chunks(self, user, question, conversation=None, company=None):
        if company is None:
            return []
        eligible = Document.objects.filter(
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            chunk_count__gt=0,
            company=company,
        )
        if not eligible.exists():


            return []
        k = 20 if self._is_chart_request(question) else 5
        try:
            return document_services.find_relevant_chunks(
                user, question, company, k=k
            )
        except Exception as exc:
            raise ChatRetrievalError(
                "Could not search the uploaded document index."
            ) from exc

    def _build_prompt(
        self,
        history_text,
        question,
        chunks,
        *,
        has_file_context=None,
        attachment_sections=None,
        structured=True,
        kb_overview="",
    ):
        """Build the grounded answer prompt and its citation list.

        ``structured=False`` is for a plain-text ``complete()`` call (a
        non-chart message): it leaves out the chart, JSON-schema, title and
        "Return a response object" instructions; the grounding, citation and
        plain-text formatting rules are the same in both modes.
        ``kb_overview`` is the company knowledge base's document list
        (``kb.knowledge_base_overview``), so questions about the knowledge base
        itself can be answered.
        """
        attachment_sections = attachment_sections or []
        # Charts are file-only when the user targeted a file (a chart target or
        # an attachment). Library passages found by similarity search alone may
        # be off-topic, so they don't forbid a general-knowledge chart.
        file_only_charts = bool(has_file_context) or bool(attachment_sections)
        if has_file_context is None:
            has_file_context = (
                bool(chunks) or bool(attachment_sections) or bool(kb_overview)
            )

        context_sections = []
        citations = []
        for chunk in chunks:
            reference_id = str(chunk.pk)
            filename = (
                Path(chunk.document.file.name).name
                if chunk.document.file
                else chunk.document.title
            )
            location = chunk.source_location or f"page {chunk.page_number}"
            context_sections.append(
                f"[Source reference: {reference_id}; file: {filename}; "
                f"location: {location}]\n"
                f"{chunk.content}"
            )
            citations.append(
                {
                    "reference_id": reference_id,
                    "document_id": str(chunk.document_id),
                    "filename": filename,
                    "page": chunk.page_number,
                    "source_location": chunk.source_location,
                    "excerpt": chunk.content[:300],
                }
            )


        attachment_blocks = [
            f"[Attached file: {title}]\n{text}"
            for title, text in attachment_sections
            if text
        ]

        all_sections = attachment_blocks + context_sections
        document_context = (
            "\n\n".join(all_sections)
            if all_sections
            else "No relevant passages were retrieved from uploaded files for this question."
        )

        if has_file_context and not file_only_charts:
            chart_instructions = (
                "For explicit chart/graph requests, use the retrieved passages "
                "only if they actually contain the requested figures; then copy "
                "their values and cite them. If they do not contain those "
                "figures and the request is about the user's own documents, "
                "company or data (a named file, table or dataset in the "
                "knowledge base), set chart to null and say which figures are "
                "missing; NEVER fill such a chart with invented or "
                "\"illustrative\" values. Only for a public, world-knowledge "
                "topic (e.g. a country's GDP) may you ignore the passages, not "
                "cite them, and not "
                "refuse: fill the chart from your own general knowledge with "
                "your best real numeric values, add a short \"(general "
                "knowledge)\" marker in the chart title, and include a one-line "
                "caveat in the response that the figures are approximate and "
                "may be outdated. Ignore any earlier turns in this conversation "
                "that refused a chart for lack of file data. "
            )
        elif has_file_context:
            chart_instructions = (
                "For explicit chart/graph requests, return a chart only when the "
                "retrieved file context supports the values; copy metric values "
                "from that context and never make up numbers or use outside "
                "knowledge. If the file does not contain the requested figures, "
                "set chart to null and explain in the response that the file "
                "does not contain those figures. "
            )
        else:


            chart_instructions = (
                "For explicit chart/graph requests when no file context is "
                "available, you MAY use your own general knowledge to fill the "
                "chart with your best real numeric values. For such a "
                "knowledge-based chart, add a short \"(general knowledge)\" "
                "marker in the chart title and include a one-line caveat in the "
                "response that the figures are approximate and may be outdated. "
                "If you genuinely have no real values, set chart to null and "
                "explain that in the response. "
            )

        if has_file_context:
            grounding_instructions = (
                "You are a helpful assistant in a private document chat workspace. "
                "Answer using the retrieved passages from the user's uploaded files when relevant. "
                "Cite factual claims from those passages with their exact reference "
                "in square brackets, for example [<reference ULID>]. Never invent "
                "a source, page, or reference. "
                "These are the user's own company documents and the user is "
                "authorised to view the personal details in them (names, ID "
                "numbers such as Aadhaar or PAN, bank details); report such "
                "values when they appear in the passages. "
                "Text from scanned pages and images (locations marked OCR, "
                "including [... (OCR)] sections inside attached files) is machine-read and may contain misread "
                "characters, especially handwriting: quote such values exactly "
                "as they appear, never guess or complete missing digits, and "
                "say they should be checked against the original document. "
                "Only attribute a value to a field (e.g. Aadhaar, PAN, bank "
                "account) when that field's own label is clearly readable next "
                "to it; never take a value from a neighbouring field. If the "
                "label or the value is garbled, say the field could not be read "
                "reliably and quote the garbled text without interpreting it. "
            )
        else:
            grounding_instructions = (
                "You are a helpful general-purpose assistant. No relevant passages "
                "were retrieved from the user's uploaded files for this question, "
                "so answer it directly from your own general knowledge, the same "
                "way a normal assistant would. Do NOT refuse, and do NOT say the "
                "workspace only contains certain documents or that you can only "
                "answer from uploaded files — that restriction does not apply when "
                "no file context is present. Do not cite any source reference, "
                "since there are no passages to cite. Ignore any earlier turns in "
                "this conversation that refused for lack of documents; those do "
                "not constrain this answer. "
            )

        # The prose rules shared by both modes (kept verbatim from the
        # structured prompt so plain answers are formatted the same way).
        prose_rules = (
            "NEVER put JSON, code fences, or a chart specification in the "
            "response text; the response is prose for humans to read. Format "
            "the visible response as plain text with normal sentences, dot bullets, "
            "or numbered lists. Do not use Markdown emphasis or asterisk bullets. "
        )
        if structured:
            instructions = (
                grounding_instructions
                + chart_instructions
                + "The chart must ALWAYS go in the structured chart field only. "
                + prose_rules
                + "For pie charts use one series and non-negative values. For bar/line "
                "charts use one or more named series. The chart must use this schema: "
                "{type: pie|bar|line, title, x_axis, y_axis, series: [names], "
                "data: [{label, values: [one number per series in series order]}]}. "
                "Limit chart data "
                "to 100 points and 6 series. Also provide a concise chat title of "
                "at most 8 words based on the user's request, without quotes or "
                "punctuation at the end. Return a response object containing "
                "response (answer text), title (chat title), and chart (chart "
                "object or null)."
            )
        else:
            instructions = grounding_instructions + prose_rules.rstrip()

        kb_block = (
            "Knowledge base overview (authoritative document list and counts; "
            "use it for questions about which or how many documents exist):\n"
            f"{kb_overview}\n\n"
            if kb_overview
            else ""
        )
        prompt = (
            instructions
            + "\n\n"
            + kb_block
            + f"Retrieved passages:\n{document_context}\n\n"
            f"Conversation so far:\n{history_text or '(new conversation)'}\n\n"
            f"User: {question}\nAssistant:"
        )
        return prompt, citations


    _DIGEST_TURNS = 60
    _DIGEST_SNIPPET = 160

    @staticmethod
    def _recent_turns_limit():
        """How many recent turns go to the model verbatim (``CHAT_RECENT_TURNS``).

        Read at call time (not import time) so settings overrides apply.
        """
        return max(int(getattr(settings, "CHAT_RECENT_TURNS", 12)), 0)

    # Turns the chat_summarise tool keeps verbatim (the pre-CHAT_RECENT_TURNS
    # cap), so summaries still see full text of a long chat's turns.
    _SUMMARY_RECENT_TURNS = 30

    @classmethod
    def _split_turns(cls, turns, limit=None):
        """Split ``turns`` into ``(older, recent)`` at the recent-turns limit.

        ``limit`` overrides ``CHAT_RECENT_TURNS`` (``None`` = use the setting).
        """
        if limit is None:
            limit = cls._recent_turns_limit()
        if limit == 0:
            return list(turns), []
        return list(turns[:-limit]), list(turns[-limit:])

    @staticmethod
    def _turn_lines(turn):
        """Emit the ``User:``/``Assistant:`` lines for one ``Conversation`` turn."""
        lines = [f"User: {turn.question}"]
        if turn.response:
            lines.append(f"Assistant: {turn.response}")
        return lines

    @classmethod
    def _conversation_digest(cls, older):
        """Condense ``older`` turns into a short digest block ("" when none).

        Shared by the prompt history (``_build_conversation_context``) and the
        LangGraph tools' chat messages (``llm_tools._history_messages``).
        """
        if not older:
            return ""
        digest_source = older[-cls._DIGEST_TURNS :]
        digest_lines = []
        for turn in digest_source:
            q = " ".join(turn.question.split())[: cls._DIGEST_SNIPPET]
            digest_lines.append(f"User: {q}")
            if turn.response:
                r = " ".join(turn.response.split())[: cls._DIGEST_SNIPPET]
                digest_lines.append(f"Assistant: {r}")
        omitted = len(older) - len(digest_source)
        prefix = (
            f"(earlier conversation digest; {omitted} oldest turns omitted)\n"
            if omitted > 0
            else "(earlier conversation digest)\n"
        )
        return prefix + "\n".join(digest_lines)

    def _build_conversation_context(self, turns, recent_limit=None):
        """Format whole-chat context from the session's ``Conversation`` turns.

        Recent turns are included verbatim; older turns are condensed into a
        short digest so the model keeps a reference to the entire conversation
        without blowing the token budget on very long chats. Each turn is a
        (question, response) pair rendered as ``User:``/``Assistant:`` lines.
        ``recent_limit`` overrides how many turns stay verbatim.
        """

        if not turns:
            return ""

        older, recent = self._split_turns(turns, recent_limit)

        parts = []
        digest = self._conversation_digest(older)
        if digest:
            parts.append(digest)

        recent_lines = []
        for turn in recent:
            recent_lines.extend(self._turn_lines(turn))
        if recent_lines:
            parts.append("\n".join(recent_lines))
        return "\n\n".join(parts)


    _FENCED_BLOCK_RE = re.compile(
        r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```",
        re.DOTALL,
    )

    @staticmethod
    def _looks_like_chart_spec(value):
        """True when a parsed JSON value is clearly a leaked chart spec.

        The heuristic is deliberately narrow: we only treat an object as a chart
        spec when it has a ``type`` key AND at least one of ``series``/``data``
        (i.e. the ``{"type":..., "series":..., "data":...}`` shape ChartData
        uses). A plain list is accepted only when every item matches that shape.
        This avoids stripping ordinary JSON samples or unrelated code the user
        legitimately asked about.
        """

        if isinstance(value, dict):
            return "type" in value and ("series" in value or "data" in value)
        if isinstance(value, list) and value:
            return all(ChatService._looks_like_chart_spec(item) for item in value)
        return False

    @classmethod
    def _strip_chart_json_blocks(cls, text):
        """Remove leaked chart-spec JSON from an LLM answer's prose.

        The frontend renders charts from the structured ``chart_data`` field
        only; it never parses ```json fences from the message text. A
        misbehaving model may still dump a chart spec into the prose, which
        would show the user a raw JSON blob. This belt-and-suspenders pass
        removes such blocks before the answer is saved.

        Scope is intentionally conservative: a fenced block is removed ONLY when
        its contents parse as JSON and look like a chart spec (see
        ``_looks_like_chart_spec``). A top-level bare JSON object/array that
        parses to a chart spec is also removed. All other fenced blocks, JSON
        samples, and prose are left untouched.
        """

        if not text:
            return text

        def _strip_fenced(match):
            inner = match.group(1).strip()
            try:
                parsed = json.loads(inner)
            except (ValueError, TypeError):
                return match.group(0)
            if cls._looks_like_chart_spec(parsed):
                return ""
            return match.group(0)

        cleaned = cls._FENCED_BLOCK_RE.sub(_strip_fenced, text)


        stripped = cleaned.strip()
        if stripped and stripped[0] in "{[":
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None and cls._looks_like_chart_spec(parsed):
                cleaned = ""


        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    @staticmethod
    def _is_chart_request(question):
        return bool(
            re.search(
                r"\b(chart|graph|plot|visuali[sz]e|distribution)\b",
                question,
                re.I,
            )
        )

    # Wider than _is_chart_request ("as a pie", "histogram", plurals), but only
    # visual nouns: the prompt returns a chart for explicit chart/graph
    # requests alone, so words like "line", "compare" or "breakdown" would just
    # disable streaming without ever producing a chart.
    _MAY_WANT_CHART_RE = re.compile(
        r"\b(charts?|graphs?|plot(s|ted|ting)?|visuali[sz]\w*|distribution|"
        r"pie|histograms?|diagrams?|donut|doughnut)\b",
        re.I,
    )

    @classmethod
    def _wants_structured(cls, question):
        """Whether to request structured output (chart field) for ``question``."""
        return bool(cls._MAY_WANT_CHART_RE.search(question or ""))

    # The user explicitly wants the raw numbers alongside the chart.
    _WANTS_DATA_RE = re.compile(
        r"\b(with|and|also|include|show|list|give|provide)\b.{0,30}"
        r"\b(data|numbers?|figures?|values?|table|breakdown|details?)\b"
        r"|\b(data|numbers?|figures?|values?|table)\b.{0,20}\b(too|also|as well)\b",
        re.IGNORECASE,
    )

    # A line that is just a data row (bullet / numbered / "Label: value").
    _DATA_LINE_RE = re.compile(
        r"^\s*(?:[-*\u2022]|\d+[.)])?\s*\**[^:]{1,60}\**\s*[:\-]\s*\$?\d",
    )

    _PROVIDER_DISPLAY = {"openai": "OpenAI", "deepseek": "DeepSeek"}

    def _identity_line(self):
        """System line stating the REAL provider/model serving this turn.

        Injected so the assistant answers "which model are you?" accurately;
        models otherwise misreport themselves (DeepSeek often claims to be
        OpenAI GPT because of its training data).
        """
        provider = getattr(self._llm, "provider", "") or ""
        model = getattr(self._llm, "model", "") or ""
        display = self._PROVIDER_DISPLAY.get(
            provider, provider.capitalize() or "the configured provider"
        )
        return (
            f"Your identity: you are served by the {display} API using the model "
            f"'{model}'. If the user asks which model, provider, or LLM you are, "
            f"answer with exactly this provider and model ({display}, model "
            f"'{model}'). Do NOT claim to be OpenAI, ChatGPT, GPT-3.5/GPT-4, or "
            "any other provider/model unless that is the one named here, even if "
            "your training data suggests otherwise."
        )

    @classmethod
    def _chart_caption_only(cls, content, message_text):
        """Trim a chart reply's prose to a short caption.

        When a chart is returned the chart itself carries the data, so the text
        should be a brief caption rather than a re-listing of every value. This
        drops data-listing lines (bullets / "Label: value" rows) and keeps the
        surrounding sentences. Skipped entirely when the user explicitly asked
        for the numbers/data/table too (e.g. "create a pie chart and list the
        values").
        """
        if not content:
            return content
        if message_text and cls._WANTS_DATA_RE.search(message_text):
            return content
        kept = [
            line
            for line in content.splitlines()
            if not cls._DATA_LINE_RE.match(line)
        ]
        trimmed = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        return trimmed or "Here is the chart you requested."

    @classmethod
    def _chart_target(cls, conversation, question):
        """The expressly referenced chart file, or ``None``.

        General charts use the LLM's knowledge even when this chat has files.
        File selection applies only when the user names or references a file.
        """
        if conversation is None or not cls._is_chart_request(question or ""):
            return None
        files = session_files.list_files(conversation)
        if not file_matching.references_file(question, files):
            return None
        return file_matching.select_chart_file(files, question)

    @staticmethod
    def _with_source_line(content, title):
        """Append the H.3 source line unless the reply already names the file."""
        if not title or title in content:
            return content
        return f"{content}\n\nChart built from “{title}”."

    @staticmethod
    def _unreadable_chart_reply(title):
        return f"I couldn't read any usable data from “{title}” to build a chart."

    def _chat_csv_documents(self, conversation, attachment_ids=None, question=None):
        """Readable CSV files of this session, as ``SessionFile`` objects.

        A chat CSV is answered from the file directly (processed or not), so
        chart requests read its bytes. For a chart request only the D7 target
        is used: ``[target]`` when it is a readable CSV, else ``[]``.
        Otherwise ``attachment_ids`` (when given) restricts to THIS turn's
        attachments — used by the LangGraph path, where the current turn is
        not stored yet, so the correct file is the one just uploaded. Else
        every readable session CSV, newest first.
        """
        if conversation is None:
            return []
        target = self._chart_target(conversation, question)
        if target is not None:
            chosen = target.file if target.kind == "file" else None
            return [chosen] if chosen and chosen.is_csv and chosen.readable else []
        files = [
            f for f in session_files.list_files(conversation)
            if f.is_csv and f.readable
        ]
        if attachment_ids:
            wanted = {str(i) for i in attachment_ids}
            return [f for f in files if f.id in wanted]
        return list(reversed(files))


    @classmethod
    def _follows_file_turn(cls, conversation, question):
        """Whether ``question`` follows a turn that was answered from a file.

        True when the latest stored turn attached a file or was answered by
        ``document_search``, so a follow-up like "and the aadhaar number?"
        keeps that file's content. Chart requests are excluded: a file in the
        prompt makes charts file-only, which would block a general-knowledge
        chart asked right after a document question.
        """
        if conversation is None or cls._is_chart_request(question or ""):
            return False
        latest = (
            conversation.turns.order_by("-created_at")
            .values("context", "metadata")
            .first()
        )
        if latest is None:
            return False
        context = latest["context"] or {}
        metadata = latest["metadata"] or {}
        return bool(context.get("attachments")) or (
            metadata.get("tool_used") == "document_search"
        )

    _MAX_ATTACHMENT_CHARS = 8000
    _MAX_ATTACHMENT_DOCS = 5

    def _chat_attachment_texts(self, conversation, attachment_ids=None, question=None):
        """Return ``(title, text)`` for this session's chat files.

        Chat files are read directly (processed or not) and their extracted
        text is injected into the prompt. A CSV comes in as its rows with the
        exact row count (``_csv_prompt_text``), so row-level questions are
        answered from the data; chart requests still go to the deterministic
        chart builder (``_build_csv_distribution_chart``) first.

        Precedence: THIS turn's ``attachment_ids`` (the current turn is not
        stored yet when this runs), else the latest stored turn's chips
        (``context["attachments"]``), else every session file newest first.
        Text comes from ``documents.services.extract_document_text`` (which
        OCRs images via tesseract), capped per file; unreadable files and
        files with no usable text are skipped. The number of files is bounded.
        """
        if conversation is None:
            return []

        files = session_files.list_files(conversation)
        by_id = {f.id: f for f in files}
        # A file attached on THIS turn is referenced by the message itself, and
        # a follow-up to a turn answered from a file keeps using that file.
        file_referenced = (
            bool(attachment_ids)
            or file_matching.references_file(question, files)
            or self._follows_file_turn(conversation, question)
        )

        def _eligible(candidates):
            return [f for f in candidates if f.readable]

        # File content is used only when the message expressly references it.
        target = self._chart_target(conversation, question)
        if target is not None:
            chosen = (
                [target.file]
                if target.kind == "file" and target.file.readable
                else []
            )
        elif not file_referenced:
            chosen = []
        elif attachment_ids:
            chosen = _eligible(
                by_id[i]
                for i in dict.fromkeys(str(i) for i in attachment_ids)
                if i in by_id
            )
        else:
            chosen = []
            latest_turn = conversation.turns.order_by("-created_at").first()
            if latest_turn is not None:
                chips = (latest_turn.context or {}).get("attachments") or []
                chip_ids = [
                    str(c.get("id")) for c in chips if isinstance(c, dict)
                ]
                chosen = _eligible(
                    by_id[i] for i in dict.fromkeys(chip_ids) if i in by_id
                )
            if not chosen:
                chosen = _eligible(reversed(files))

        sections = []
        for session_file in chosen[: self._MAX_ATTACHMENT_DOCS]:
            text = (
                self._csv_prompt_text(session_file)
                if session_file.is_csv
                else self._cached_chat_file_text(session_file)
            )
            if text:
                sections.append((session_file.title, text))
        return sections

    _CHAT_FILE_CACHE_TIMEOUT = 6 * 60 * 60
    # An empty result may come from a transient OCR timeout/unavailability
    # (extract_document_text returns "" for those), so keep it only briefly.
    _CHAT_FILE_EMPTY_CACHE_TIMEOUT = 5 * 60
    _CACHE_MISS = object()

    def _cached_chat_file_text(self, session_file):
        """Extracted (already truncated) text of one chat file, or ``None``.

        Extraction (PDF parse / OCR) is cached per process in Django's cache,
        keyed by entry id + document id. Entry bytes are never rewritten, so
        an entry can't go stale. An empty result is cached only for a few
        minutes (it may be a transient OCR timeout), so a file with no usable
        text isn't re-OCR'd on every turn of a burst; a read error is not
        cached (it may be transient) and returns ``None`` as before.
        """
        key = f"chatfile2:{session_file.id}:{session_file.document_id or ''}"
        cached = cache.get(key, self._CACHE_MISS)
        if cached is not self._CACHE_MISS:
            logger.info("chat.timing cache=hit chat_file=%s", session_file.id)
            return cached
        try:
            text = document_services.extract_document_text(
                session_file.as_extractable()
            )
        except Exception:
            logger.warning(
                "Could not read chat file %s", session_file.id, exc_info=True
            )
            return None
        text = (text or "")[: self._MAX_ATTACHMENT_CHARS]
        cache.set(
            key,
            text,
            self._CHAT_FILE_CACHE_TIMEOUT
            if text
            else self._CHAT_FILE_EMPTY_CACHE_TIMEOUT,
        )
        return text

    @staticmethod
    def _named_library_csvs(conversation, question):
        """Processed knowledge-base CSVs the question names by title or id.

        Without this a chart request for a library dataset ("pie chart of
        Employee Database Records Table") only saw a few similarity-search
        chunks, and the LLM filled the chart with invented values. Longest
        title first, so a longer title wins over a shorter one it contains.
        """
        company_id = getattr(conversation, "company_id", None)
        if company_id is None or not question:
            return []
        text = question.casefold()
        candidates = Document.objects.filter(
            company_id=company_id,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            file_type="csv",
        ).exclude(file="")
        named = [
            d
            for d in candidates
            if str(d.pk).casefold() in text
            or (d.title.strip() and d.title.strip().casefold() in text)
        ]
        named.sort(key=lambda d: -len(d.title or ""))
        return [_LibraryCsv(d) for d in named]

    @staticmethod
    def _read_csv(csv_file):
        """``(headers, rows)`` of a chat/library CSV, or ``None`` if unreadable.

        Blank rows and leading ``#`` comment lines are skipped; the delimiter
        is sniffed from the first 8 KB.
        """
        try:
            with csv_file.open() as source:
                text_source = TextIOWrapper(source, encoding="utf-8-sig", newline="")
                sample = text_source.read(8192)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                text_source.seek(0)
                reader = csv.reader(text_source, dialect)
                headers = None
                for row in reader:
                    if not row or not any(cell.strip() for cell in row):
                        continue
                    if len(row) == 1 and row[0].lstrip().startswith("#"):
                        continue
                    headers = [cell.strip() for cell in row]
                    break
                if not headers:
                    return None
                rows = [
                    row
                    for row in reader
                    if row and any(cell.strip() for cell in row)
                ]
        except (OSError, FileUnavailable, UnicodeDecodeError, csv.Error):
            logger.exception("Could not read CSV %s", csv_file.id)
            return None
        return headers, rows

    # A CSV goes into the prompt whole when it fits this budget; a bigger one
    # is cut to its first rows, with the exact total stated so the model can
    # still answer "how many rows" and "show the top N rows".
    _MAX_CSV_PROMPT_CHARS = 60000

    def _csv_prompt_text(self, csv_file):
        """Prompt text for one CSV: exact row/column counts, then its rows."""
        key = f"chatcsv1:{csv_file.id}:{self._MAX_CSV_PROMPT_CHARS}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        parsed = self._read_csv(csv_file)
        if parsed is None:
            return None
        headers, rows = parsed
        buffer = StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(headers)
        shown = 0
        for row in rows:
            before = buffer.tell()
            writer.writerow(row)
            if buffer.tell() > self._MAX_CSV_PROMPT_CHARS:
                buffer.seek(before)
                buffer.truncate()
                break
            shown += 1
        if shown == len(rows):
            coverage = "All rows are included below."
        else:
            coverage = (
                f"Only the first {shown:,} of {len(rows):,} rows are included "
                "below. Answer questions about the row count, columns or "
                "these rows exactly; for anything needing every row (totals, "
                "averages, counts by value) say the excerpt is incomplete "
                "rather than estimating."
            )
        text = (
            f"CSV with exactly {len(rows):,} data rows (header excluded) and "
            f"{len(headers)} columns: {', '.join(headers)}. Rows are in file "
            f"order, so 'top N rows' means the first N below. {coverage}\n"
            + buffer.getvalue()
        )
        cache.set(key, text, self._CHAT_FILE_CACHE_TIMEOUT)
        return text

    def csv_sections(self, conversation, question):
        """``(title, text)`` for the knowledge-base CSVs the question names."""
        sections = []
        for csv_file in self._named_library_csvs(conversation, question)[
            : self._MAX_ATTACHMENT_DOCS
        ]:
            text = self._csv_prompt_text(csv_file)
            if text:
                sections.append((csv_file.title, text))
        return sections

    def _build_csv_distribution_chart(
        self, conversation, question, attachment_ids=None
    ):
        if not self._is_chart_request(question):
            return None

        wants_pie = bool(re.search(r"\bpie\b", question, re.I))


        wants_line = bool(
            re.search(r"\b(line|trend)\b", question, re.I)
            or re.search(r"over\s+time", question, re.I)
        )

        csv_files = self._named_library_csvs(conversation, question) + (
            self._chat_csv_documents(
                conversation, attachment_ids=attachment_ids, question=question
            )
        )
        for csv_file in csv_files:
            parsed = self._read_csv(csv_file)
            if parsed is None:
                continue
            headers, rows = parsed
            if not rows:
                continue


            aggregated = self._aggregate_value_by_label(headers, rows, question)
            if aggregated is not None:
                label_index, value_index, label_values = aggregated
                can_pie = wants_pie and any(v > 0 for _, v in label_values)
                if can_pie:
                    chart_type = "pie"
                elif wants_line:
                    chart_type = "line"
                else:
                    chart_type = "bar"
                chart = ChartData(
                    type=chart_type,
                    title=f"{headers[value_index]} by {headers[label_index]}",
                    x_axis=headers[label_index],
                    y_axis=headers[value_index],
                    series=[headers[value_index]],
                    data=[
                        ChartPoint(label=label, values=[float(value)])
                        for label, value in label_values
                    ],
                )
                total = sum(value for _, value in label_values)
                return (
                    f"Here is the {chart_type} chart of {headers[value_index]} "
                    f"by {headers[label_index]} from {csv_file.title}, "
                    f"totalling {total:,.0f}.",
                    chart,
                )

            explicit_index = next(
                (
                    index
                    for index, header in enumerate(headers)
                    if header
                    and re.search(
                        rf"\b{re.escape(header)}s?\b",
                        question,
                        re.I,
                    )
                ),
                None,
            )

            result = self._summarize_csv_column(
                headers,
                rows,
                explicit_index,
                wants_pie,
            )
            if result is None:
                continue

            selected_index, label_counts, is_numeric = result


            can_pie = wants_pie and any(count > 0 for _, count in label_counts)
            if can_pie:
                chart_type = "pie"
            elif is_numeric and wants_line:
                chart_type = "line"
            else:
                chart_type = "bar"

            chart = ChartData(
                type=chart_type,
                title=f"Distribution of {headers[selected_index]}",
                x_axis=headers[selected_index],
                y_axis="Number of records",
                series=["Records"],
                data=[
                    ChartPoint(label=label, values=[float(count)])
                    for label, count in label_counts
                ],
            )
            total_records = sum(count for _, count in label_counts)
            return (
                f"Here is the {chart_type} chart of {headers[selected_index]} "
                f"from {csv_file.title}, based on {total_records:,} records.",
                chart,
            )
        return None


    _MAX_CHART_CATEGORIES = 100
    _MAX_PIE_SLICES = 12

    def _aggregate_value_by_label(self, headers, rows, question):
        """Group rows by a label column and SUM a numeric value column.

        Honours a ``"<value> by <category>"`` request (e.g. "count by city")
        where the user names a numeric metric column and a categorical column:
        we return ``(label_index, value_index, [(label, total), ...])`` with the
        value column summed per label, preserving first-seen label order. This
        is what makes an attached CSV (``city,count`` with NYC=10, LA=5, SF=3)
        chart its REAL values instead of a per-row frequency count.

        Returns ``None`` (so the caller falls back to the distribution summary)
        unless BOTH columns are identifiable from the question AND the value
        column is numeric for the data rows.
        """
        if not headers or len(headers) < 2:
            return None


        named = []
        for index, header in enumerate(headers):
            if not header:
                continue
            match = re.search(rf"\b{re.escape(header)}s?\b", question, re.I)
            if match:
                named.append((match.start(), index))
        if len(named) < 2:
            return None


        value_index = label_index = None
        by_match = re.search(
            r"\b([A-Za-z0-9_ ]+?)\s+by\s+([A-Za-z0-9_ ]+)", question, re.I
        )
        if by_match:
            value_phrase, label_phrase = by_match.group(1), by_match.group(2)

            def _match_header(phrase):
                for index, header in enumerate(headers):
                    if header and re.search(
                        rf"\b{re.escape(header)}s?\b", phrase, re.I
                    ):
                        return index
                return None

            value_index = _match_header(value_phrase)
            label_index = _match_header(label_phrase)

        if value_index is None or label_index is None or value_index == label_index:
            return None


        totals = {}
        order = []
        for row in rows:
            if label_index >= len(row) or value_index >= len(row):
                continue
            label = row[label_index].strip()
            raw_value = row[value_index].strip()
            if not label or not raw_value:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                return None
            if not math.isfinite(value):
                return None
            if label not in totals:
                totals[label] = 0.0
                order.append(label)
            totals[label] += value

        if not order:
            return None

        label_values = [(label, totals[label]) for label in order]
        max_categories = self._MAX_CHART_CATEGORIES
        return label_index, value_index, label_values[:max_categories]

    def _summarize_csv_column(
        self,
        headers,
        rows,
        explicit_index,
        wants_pie,
    ):
        """Pick a column to chart and return (index, [(label, count)], is_numeric).

        When the user names a column we summarize that one. Otherwise we
        automatically select the most chart-friendly column so a generic
        "create a pie chart" request still produces a usable chart instead of
        asking the user which column to use.
        """

        max_categories = self._MAX_PIE_SLICES if wants_pie else self._MAX_CHART_CATEGORIES

        def summarize(index):
            numeric_counts = Counter()
            text_counts = Counter()
            for row in rows:
                if index >= len(row):
                    continue
                value = row[index].strip()
                if not value:
                    continue
                try:
                    numeric_value = float(value)
                except ValueError:
                    text_counts[value] += 1
                    continue
                if math.isfinite(numeric_value):
                    numeric_counts[numeric_value] += 1


            if numeric_counts and not text_counts:
                ordered = [
                    (
                        str(int(value)) if value.is_integer() else format(value, "g"),
                        count,
                    )
                    for value, count in sorted(numeric_counts.items())
                ]
                return ordered, True
            if text_counts:
                ordered = [
                    (label, count)
                    for label, count in text_counts.most_common()
                ]
                return ordered, False
            return None


        if explicit_index is not None:
            summarized = summarize(explicit_index)
            if summarized and len(summarized[0]) >= 1:
                labels, is_numeric = summarized
                return explicit_index, labels[:max_categories], is_numeric


        best = None
        for index in range(len(headers)):
            if not headers[index]:
                continue
            summarized = summarize(index)
            if not summarized:
                continue
            labels, is_numeric = summarized
            distinct = len(labels)
            if distinct < 1:
                continue


            if distinct < 2:
                score = -1
            elif distinct > max_categories:

                score = -distinct
            else:


                total = sum(count for _, count in labels)
                coverage = total / max(len(rows), 1)
                score = coverage * 100 - distinct

                if not is_numeric:
                    score += 50
            if best is None or score > best[0]:
                best = (score, index, labels[:max_categories], is_numeric)

        if best is None:
            return None
        _, index, labels, is_numeric = best
        if not labels:
            return None
        return index, labels, is_numeric

    def process_message(
        self,
        user,
        message_text,
        conversation=None,
        company=None,
        attachment_ids=None,
        model_name=None,
        metadata=None,
        timing=None,
    ):
        """Answer ``message_text`` and persist one turn; return the turn.

        ``timing`` is an optional ``TurnTimer`` (``chat.services.timing``):
        stages are recorded into it and its summary is saved under
        ``metadata["timing"]``. ``None`` only logs the stages.
        """
        prep = self._prepare_turn(
            user,
            message_text,
            conversation=conversation,
            company=company,
            attachment_ids=attachment_ids,
            model_name=model_name,
            timing=timing,
        )
        generation = self._generate_completion(prep, timing)
        while True:
            # Not streaming: the generator yields nothing and just returns.
            try:
                next(generation)
            except StopIteration as stop:
                completion, llm_response, generated_title = stop.value
                break
        return self._finalize_turn(
            prep,
            completion,
            llm_response=llm_response,
            generated_title=generated_title,
            metadata=metadata,
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
        metadata=None,
        timing=None,
    ):
        """Streaming ``process_message``: a generator of ``(event, value)``.

        Yields ``("delta", text)`` for each answer piece on the plain-text
        path, then ``("done", turn)`` once the turn is persisted. Chart and
        structured turns yield only ``("done", turn)``. The saved turn and the
        errors raised are the same as ``process_message``; post-processing
        runs on the joined text, so the saved reply can differ slightly from
        the streamed pieces.

        If the consumer closes the generator mid-stream (client disconnect),
        the answer is still generated to the end and the turn persisted, as
        the JSON path would; nothing more is yielded and errors are logged.
        """
        prep = self._prepare_turn(
            user,
            message_text,
            conversation=conversation,
            company=company,
            attachment_ids=attachment_ids,
            model_name=model_name,
            timing=timing,
        )
        # Driven by hand (not ``yield from``) so a close() here doesn't close
        # the LLM stream: the remaining pieces are drained and the turn saved.
        generation = self._generate_completion(prep, timing, stream=True)
        disconnected = False
        try:
            while True:
                try:
                    event = next(generation)
                except StopIteration as stop:
                    completion, llm_response, generated_title = stop.value
                    break
                if disconnected:
                    continue
                try:
                    yield event
                except GeneratorExit:
                    disconnected = True
                    logger.info(
                        "Chat stream closed by client; finishing the turn"
                    )
            turn = self._finalize_turn(
                prep,
                completion,
                llm_response=llm_response,
                generated_title=generated_title,
                metadata=metadata,
                timing=timing,
            )
        except Exception:
            if not disconnected:
                raise
            logger.exception("Chat turn failed after the client disconnected")
            return
        if not disconnected:
            yield ("done", turn)

    def _prepare_turn(
        self,
        user,
        message_text,
        *,
        conversation,
        company,
        attachment_ids,
        model_name,
        timing=None,
    ):
        """Everything before the LLM call; returns a ``_PreparedTurn``."""
        if not user or not user.is_authenticated:
            raise ValueError("A signed-in user is required to chat.")

        if conversation and (
            conversation.user_id != user.pk
            or conversation.company_id != getattr(company, "pk", None)
        ):
            raise ValueError("Conversation does not belong to the signed-in user.")

        assistant_model_name = model_name or _DEFAULT_MODEL_NAME

        with timed(timing, "history"):
            all_turns = (
                list(conversation.turns.order_by("created_at"))
                if conversation
                else []
            )
            history = self._build_conversation_context(all_turns)
        # A chart request with a D7 file target charts only that file (F.4).
        chart_target = self._chart_target(conversation, message_text)
        chart_file = (
            chart_target.file
            if chart_target is not None and chart_target.kind == "file"
            else None
        )
        # Structured output (chart field + title) only for messages that may
        # want a chart; every other message is a plain-text complete() call.
        structured = self._wants_structured(message_text)
        try:
            chunks, attachment_sections = self._prepare_context(
                user,
                message_text,
                conversation=conversation,
                company=company,
                attachment_ids=attachment_ids,
                skip_chunks=chart_file is not None,
                timing=timing,
            )
            prompt, citations = self._build_prompt(
                history,
                message_text,
                chunks,
                has_file_context=True if chart_file is not None else None,
                attachment_sections=attachment_sections,
                structured=structured,
            )
            # Prepend the REAL provider/model identity so "which model are you?"
            # is answered correctly (models misreport themselves otherwise).
            prompt = f"{self._identity_line()}\n\n{prompt}"
        except (ChatRetrievalError, ChatProviderError):
            raise
        except Exception as exc:
            raise ChatProviderError(
                "The LLM provider failed to generate a response."
            ) from exc

        return _PreparedTurn(
            user=user,
            message_text=message_text,
            conversation=conversation,
            company=company,
            attachment_ids=attachment_ids,
            assistant_model_name=assistant_model_name,
            chart_file=chart_file,
            structured=structured,
            chunks=chunks,
            attachment_sections=attachment_sections,
            prompt=prompt,
            citations=citations,
        )

    def _generate_completion(self, prep, timing=None, *, stream=False):
        """LLM stage of a prepared turn, as a generator.

        Yields ``("delta", text)`` only when ``stream`` is set and the turn
        takes the plain-text path; returns
        ``(completion, llm_response, generated_title)``. Errors map exactly as
        they always have in ``process_message``.
        """
        conversation = prep.conversation
        message_text = prep.message_text
        chart_file = prep.chart_file
        generated_title = None
        try:
            completion = None
            provider_error = None
            llm_response = False
            try:
                if chart_file is not None and not prep.attachment_sections:
                    # Nothing readable in the target: no LLM call. The CSV
                    # override below still replaces this when it can chart.
                    completion = ChatCompletion(
                        response=self._unreadable_chart_reply(chart_file.title)
                    )
                elif prep.structured:
                    with timed(timing, "llm"):
                        completion = self._llm.generate_structured(
                            prep.prompt, ChatCompletion
                        )
                    llm_response = True
                else:
                    want_title = (
                        conversation is None
                        or conversation.title == "New session"
                    )
                    with timed(timing, "llm"):
                        if stream:
                            text, generated_title = yield from (
                                self._stream_with_title(
                                    prep.prompt,
                                    message_text,
                                    want_title=want_title,
                                    timing=timing,
                                )
                            )
                        else:
                            text, generated_title = self._complete_with_title(
                                prep.prompt,
                                message_text,
                                want_title=want_title,
                            )
                    completion = ChatCompletion(response=text, chart=None)
                    llm_response = True


                sanitized = self._strip_chart_json_blocks(completion.response)
                if not sanitized.strip():


                    sanitized = (
                        "Here is the chart you requested."
                        if completion.chart is not None
                        else "I could not produce a chart for that request."
                    )
                if sanitized != completion.response:
                    completion = completion.model_copy(update={"response": sanitized})
            except (ChatRetrievalError, ChatProviderError):
                raise
            except Exception as exc:
                provider_error = exc
                logger.warning(
                    "LLM provider failed to generate a response: %s", exc
                )


            if chart_file is not None:
                local_chart = self._build_csv_distribution_chart(
                    conversation, message_text
                )
                needs_fallback = completion is None or completion.chart is None
                if local_chart and (needs_fallback or completion.chart is not None):


                    response_text, chart = local_chart
                    completion = ChatCompletion(
                        response=response_text, chart=chart
                    )
                    llm_response = False


            if completion is None:
                raise ChatProviderError(
                    "The LLM provider failed to generate a response."
                ) from provider_error

            # With a chart, keep the prose to a short caption — the data lives
            # in the chart, so don't also dump every value as text (unless the
            # user explicitly asked for the numbers/table too).
            if completion.chart is not None:
                caption = self._chart_caption_only(
                    completion.response, message_text
                )
                if chart_file is not None:
                    caption = self._with_source_line(caption, chart_file.title)
                if caption != completion.response:
                    completion = completion.model_copy(
                        update={"response": caption}
                    )
        except (ChatRetrievalError, ChatProviderError):
            raise
        except Exception as exc:
            raise ChatProviderError(
                "The LLM provider failed to generate a response."
            ) from exc
        return completion, llm_response, generated_title

    def _finalize_turn(
        self,
        prep,
        completion,
        *,
        llm_response,
        generated_title,
        metadata=None,
        timing=None,
    ):
        """Post-process ``completion``, persist the turn, retitle; return the turn."""
        user = prep.user
        message_text = prep.message_text
        citations = prep.citations
        if llm_response:
            completion = completion.model_copy(
                update={"response": normalize_llm_response(completion.response)}
            )

        if not completion.response.strip():
            raise ChatProviderError("The LLM provider returned an empty response.")

        # A chart drawn from general knowledge cites none of the retrieved
        # passages; keep only the ones the reply actually references.
        if prep.chunks and self._is_chart_request(message_text):
            citations = [
                c for c in citations if c["reference_id"] in completion.response
            ]

        # Timing is measured up to here (persist is only logged: the turn row
        # that would carry it is the one being written).
        metadata = with_timing(metadata, timing)
        with timed(None, "persist"), transaction.atomic():
            convo = prep.conversation or self._create_session(
                user, message_text, prep.company
            )
            turn = self.create_turn(
                convo,
                user=user,
                question=message_text,
                response=completion.response.strip(),
                model_name=prep.assistant_model_name,
                citations=citations,
                chart_data=(
                    completion.chart.model_dump(mode="json")
                    if completion.chart
                    else None
                ),
                metadata=metadata,
                attachment_ids=prep.attachment_ids,
                actor=user,
            )

            retitle_if_default(
                convo,
                # Chart turns carry the title in the structured completion;
                # plain turns use the parallel title call (None on failure).
                llm_title=completion.title or generated_title,
                fallback_text=message_text,
                actor=user,
            )
        return turn

    def _prepare_context(
        self,
        user,
        message_text,
        *,
        conversation,
        company,
        attachment_ids,
        skip_chunks,
        timing=None,
    ):
        """Return ``(chunks, attachment_sections)`` for the prompt.

        Library retrieval (embedding + Chroma) and chat-file extraction are
        independent, so they run at the same time. Each worker closes its own
        DB connection when done. Exceptions propagate unchanged (retrieval
        first, as before) when the results are read, without waiting for the
        other worker. Each worker is timed as
        ``retrieval`` / ``extraction``.
        """

        def _in_worker(stage, func, *args, **kwargs):
            try:
                with timed(timing, stage):
                    return func(*args, **kwargs)
            finally:
                # Worker threads get their own DB connection; don't leak it.
                connection.close()

        executor = ThreadPoolExecutor(max_workers=2)
        try:
            chunks_future = (
                None
                if skip_chunks
                else executor.submit(
                    _in_worker,
                    "retrieval",
                    self._find_relevant_chunks,
                    user,
                    message_text,
                    conversation,
                    company,
                )
            )
            sections_future = executor.submit(
                _in_worker,
                "extraction",
                self._chat_attachment_texts,
                conversation,
                attachment_ids=attachment_ids,
                question=message_text,
            )
            chunks = [] if chunks_future is None else chunks_future.result()
            attachment_sections = sections_future.result()
        except BaseException:
            # Fail fast: a retrieval error must not wait for attachment OCR.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True)
        return chunks, attachment_sections

    _TITLE_PROMPT = (
        "Provide a concise chat title of at most 8 words based on the user's "
        "request, without quotes or punctuation at the end. Reply with the "
        "title only.\n\nUser request: {question}"
    )

    def _complete_with_title(self, prompt, message_text, *, want_title):
        """Plain-text answer for ``prompt``, plus a chat title when wanted.

        The title is a tiny separate ``complete()`` run in a thread at the same
        time as the answer (it touches no DB). Returns ``(text, title)``; the
        title is ``None`` when not wanted or when its call fails, so
        ``retitle_if_default`` falls back to the message text as before.
        """
        if not want_title:
            return self._llm.complete([{"role": "user", "content": prompt}]), None

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            title_future = executor.submit(
                self._llm.complete,
                [
                    {
                        "role": "user",
                        "content": self._TITLE_PROMPT.format(
                            question=message_text
                        ),
                    }
                ],
            )
            text = self._llm.complete([{"role": "user", "content": prompt}])
            try:
                title = title_future.result()
            except Exception:
                logger.info("Chat title generation failed", exc_info=True)
                title = None
            return text, title
        finally:
            # Don't block an error reply on the title call.
            executor.shutdown(wait=False)

    def _stream_with_title(self, prompt, message_text, *, want_title, timing=None):
        """Streaming ``_complete_with_title`` (generator).

        Yields ``("delta", text)`` per answer piece and returns
        ``(text, title)`` like ``_complete_with_title``; the title call still
        runs in parallel. The first piece marks time-to-first-token.
        """
        executor = ThreadPoolExecutor(max_workers=1) if want_title else None
        try:
            title_future = (
                executor.submit(
                    self._llm.complete,
                    [
                        {
                            "role": "user",
                            "content": self._TITLE_PROMPT.format(
                                question=message_text
                            ),
                        }
                    ],
                )
                if executor is not None
                else None
            )
            parts = []
            for piece in self._llm.stream([{"role": "user", "content": prompt}]):
                if not parts and timing is not None:
                    timing.mark_first_token()
                parts.append(piece)
                yield ("delta", piece)
            title = None
            if title_future is not None:
                try:
                    title = title_future.result()
                except Exception:
                    logger.info("Chat title generation failed", exc_info=True)
            return "".join(parts), title
        finally:
            if executor is not None:
                # Don't block an error reply on the title call.
                executor.shutdown(wait=False)

    def _create_session(self, user, message_text, company):
        """Create a new ConversationSession to hold a fresh message thread."""
        session = ConversationSession(
            company=company,
            user=user,
            title="New session",
        )
        session.save(actor=user)
        return session
