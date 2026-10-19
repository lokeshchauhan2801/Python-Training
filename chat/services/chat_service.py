"""Chat orchestration service (the LLM dependency-inversion seam).

``ChatService`` depends on the ``LLMClient`` *contract*, injected via its
constructor. The default is a real ``LLMClient`` whose backend/key is only
touched on the first ``complete``/``generate_structured`` call, so constructing
a ``ChatService`` never requires a key. Tests inject a fake client.

Two surfaces live here side by side:

* ``ask``/``summarize``/``_session_messages`` — the existing turn-pair
  (``Conversation``) flow driven by the DRF API and in-admin chat.
* ``process_message`` + its helpers — the ported RAG pipeline (ported/adapted
  from the OLD ``chat/services.py``): document-grounded retrieval via
  ``documents.services.find_relevant_chunks``, a Pydantic-validated structured
  completion carrying an optional chart, a context-aware repeated-query cache,
  a deterministic CSV chart fallback, and ``Message`` persistence. It is
  additive — it never changes the ``ask``/``summarize`` behaviour the existing
  23 chat tests pin.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
from collections import Counter
from io import TextIOWrapper
from pathlib import Path
from typing import Literal

from django.db import transaction
from pydantic import BaseModel as PydanticBaseModel, Field, model_validator

from basics.llm import LLMClient
from documents import services as document_services
from documents.models import Document, DocumentChunk

from chat.models import Conversation, ConversationSession, Message
from chat.retitle import retitle_if_default

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful assistant. Summarize the following conversation "
    "concisely, capturing the key questions and conclusions."
)

# Model literal matching settings.LLM_MODEL; stamped on messages whose author is
# the assistant when the service does not supply a more specific name.
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


class ChatService:
    def __init__(self, llm_client: LLMClient | None = None):
        # Default to a real client, but build no backend/key at construct time.
        self._llm = llm_client or LLMClient()

    # ------------------------------------------------------------------ #
    # Existing turn-pair surface (unchanged behaviour).                  #
    # ------------------------------------------------------------------ #
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

        answer = self._llm.complete(messages)

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
        # full_clean enforces the company-match invariant (and field validation)
        # before the save-time backstop re-checks it.
        turn.full_clean()
        turn.save(actor=actor or user)
        return turn

    def summarize(self, *, session, actor=None) -> str:
        """Summarize a session; persist the summary onto the session."""
        messages = [{"role": "system", "content": _SUMMARY_SYSTEM_PROMPT}]
        messages.extend(self._session_messages(session))

        summary = self._llm.complete(messages)

        session.summary = summary
        session.model_name = self._llm.model
        session.save(
            actor=actor,
            update_fields=["summary", "model_name", "updated_at", "updated_by"],
        )
        return summary

    # ------------------------------------------------------------------ #
    # RAG message pipeline (ported from OLD chat/services.py).           #
    # ------------------------------------------------------------------ #
    def create_message(
        self,
        conversation,
        role,
        content,
        model_name=None,
        citations=None,
        chart_data=None,
        context_signature=None,
        actor=None,
    ):
        """Persist a ``Message`` on ``conversation`` with the company stamped.

        The message always inherits ``conversation.company`` (the company-match
        invariant is enforced in ``Message.save()``). The save is actor-aware so
        the audit columns are stamped from the acting user.
        """
        message = Message(
            company=conversation.company,
            conversation=conversation,
            role=role,
            content=content,
            model_name=model_name or _DEFAULT_MODEL_NAME,
            citations=citations or [],
            chart_data=chart_data,
            context_signature=context_signature,
        )
        message.save(actor=actor)
        return message

    def _context_signature(self, user, company):
        """Return a cheap hash of the retrievable document context.

        The signature captures what could change retrieval for THIS company so
        the repeated-query cache can tell whether the document context changed
        between two identical questions. It is derived from the SAME
        eligible-document scope retrieval uses (``source=LIBRARY``,
        ``status=READY``, ``chunk_count>0``, filtered by ``company``), so an
        unrelated company's upload never affects another company's signature.

        Each eligible document contributes ``"<pk>:<updated_at>:<chunk_count>"``
        so an upload becoming READY (new pk), a reprocess (``updated_at`` and/or
        ``chunk_count`` move) and a delete (pk gone) all change the hash. The
        query loads only three small columns and never reads chunk bodies. An
        empty eligible set yields a stable, non-null hash so two identical
        no-file questions still share a signature and stay cacheable.
        """
        documents = Document.objects.filter(
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            chunk_count__gt=0,
        )
        if company is not None:
            documents = documents.filter(company=company)

        rows = documents.order_by("pk").values_list("pk", "updated_at", "chunk_count")
        payload = "|".join(
            f"{pk}:{updated_at.isoformat() if updated_at else ''}:{chunk_count}"
            for pk, updated_at, chunk_count in rows
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _find_relevant_chunks(self, user, question, conversation=None, company=None):
        """Retrieve grounding passages from the company's library documents.

        Delegates to ``documents.services.find_relevant_chunks`` (which owns the
        embed-query + Chroma similarity-search + owner-scoped re-mapping). Chart-
        ish questions widen the ``k`` so the deterministic chart builder has more
        to work with. Returns an empty list when no company is in scope (there is
        nothing owner-scoped to search).

        EARLY RETURN (matches the OLD project): if there are NO eligible library
        documents (``source=LIBRARY``, ``status=READY``, ``chunk_count>0`` for
        this company) we return ``[]`` immediately and NEVER touch Chroma. With
        an empty/never-created vector collection this is the difference between a
        normal general-knowledge answer and a spurious retrieval 502. The
        ``ChatRetrievalError`` path then only fires when documents DO exist but
        the search genuinely fails.
        """
        if company is None:
            return []
        eligible = Document.objects.filter(
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            chunk_count__gt=0,
            company=company,
        )
        if not eligible.exists():
            # No indexed library documents => zero relevant chunks (NORMAL),
            # never a retrieval error. Do not call embed_query / Chroma.
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
    ):
        # Whether the session has file/document context to ground a chart in.
        # Defaults to "do we have retrieved chunks?" so the single existing call
        # site keeps working; tests may pass the flag explicitly.
        #
        # ``attachment_sections`` carries pre-extracted text from chat-attached
        # files (``[(title, text), ...]``). Chat attachments are NOT embedded, so
        # their content is folded directly into the grounding passages here. When
        # any attachment text is present we also treat the turn as having file
        # context (strict, no-outside-numbers mode) so the model answers FROM the
        # file instead of general knowledge. Attachment blocks are NOT cited
        # (they are not chunk-cited); citations stay sourced from ``chunks``.
        attachment_sections = attachment_sections or []
        if has_file_context is None:
            has_file_context = bool(chunks) or bool(attachment_sections)

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

        # Chat-attached files are read directly (never embedded) and folded in
        # as labelled, un-cited blocks ahead of any retrieved library chunks.
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

        if has_file_context:
            # FILE case: stay strict. The chart may only reflect the retrieved
            # file context; copy its numbers and never invent. If the file does
            # not contain the requested figures, set chart to null and say the
            # file does not contain them (do NOT fall back to general knowledge).
            chart_instructions = (
                "For explicit chart/graph requests, return a chart only when the "
                "retrieved file context supports the values; copy metric values "
                "from that context and never make up numbers or use outside "
                "knowledge. If the file does not contain the requested figures, "
                "set chart to null and explain in the response that the file "
                "does not contain those figures. "
            )
        else:
            # NO-FILE case: the user asked for a chart but there is no file to
            # ground it in. The model MAY use its own general knowledge to fill
            # the structured chart with its best real numeric values. For such a
            # knowledge-base chart it must add a short "(general knowledge)"
            # marker in the chart title and a one-line caveat in the response
            # that the figures are approximate and may be outdated. If it has no
            # real values, set chart to null and say so in the response.
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

        prompt = (
            "You are a helpful assistant in a private document chat workspace. "
            "Answer using the retrieved passages from the user's uploaded files when relevant. "
            "Cite factual claims from those passages with their exact reference "
            "in square brackets, for example [<reference ULID>]. Never invent "
            "a source, page, or reference. "
            + chart_instructions
            + "The chart must ALWAYS go in the structured chart field only. "
            "NEVER put JSON, code fences, or a chart specification in the "
            "response text; the response is prose for humans to read. "
            "For pie charts use one series and non-negative values. For bar/line "
            "charts use one or more named series. The chart must use this schema: "
            "{type: pie|bar|line, title, x_axis, y_axis, series: [names], "
            "data: [{label, values: [one number per series in series order]}]}. "
            "Limit chart data "
            "to 100 points and 6 series. Also provide a concise chat title of "
            "at most 8 words based on the user's request, without quotes or "
            "punctuation at the end. Return a response object containing "
            "response (answer text), title (chat title), and chart (chart "
            "object or null).\n\n"
            f"Retrieved passages:\n{document_context}\n\n"
            f"Conversation so far:\n{history_text or '(new conversation)'}\n\n"
            f"User: {question}\nAssistant:"
        )
        return prompt, citations

    # How many of the most recent turns to include verbatim. Everything older
    # is condensed into a digest so the LLM still references the whole chat.
    _RECENT_TURNS = 30
    _DIGEST_TURNS = 60
    _DIGEST_SNIPPET = 160

    def _build_conversation_context(self, messages):
        """Format whole-chat context from Postgres message history.

        Recent turns are included verbatim; older turns are condensed into a
        short digest so the model keeps a reference to the entire conversation
        without blowing the token budget on very long chats.
        """

        if not messages:
            return ""

        recent = messages[-self._RECENT_TURNS :]
        older = messages[: -self._RECENT_TURNS] if len(messages) > self._RECENT_TURNS else []

        parts = []
        if older:
            # Oldest-first digest, capped to the most recent of the old turns so
            # the digest itself stays bounded on extremely long conversations.
            digest_source = older[-self._DIGEST_TURNS :]
            digest_lines = []
            for message in digest_source:
                snippet = " ".join(message.content.split())[: self._DIGEST_SNIPPET]
                digest_lines.append(f"{message.role.title()}: {snippet}")
            omitted = len(older) - len(digest_source)
            prefix = (
                f"(earlier conversation digest; {omitted} oldest turns omitted)\n"
                if omitted > 0
                else "(earlier conversation digest)\n"
            )
            parts.append(prefix + "\n".join(digest_lines))

        parts.append(
            "\n".join(
                f"{message.role.title()}: {message.content}" for message in recent
            )
        )
        return "\n\n".join(parts)

    # Fenced code block containing (optionally) a language hint, e.g. ```json.
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

        # Also handle a bare (unfenced) top-level JSON chart spec: find the first
        # balanced {...} or [...] block and drop it if it parses to a chart spec.
        stripped = cleaned.strip()
        if stripped and stripped[0] in "{[":
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None and cls._looks_like_chart_spec(parsed):
                cleaned = ""

        # Collapse the blank lines left behind by removed blocks.
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

    def _chat_csv_documents(self, conversation):
        """Chat-source CSV documents attached to this session.

        A chat-attached CSV is chat data only (never embedded), so chart
        requests about it are answered from the file directly. Attachments are
        linked to a session via ``documents.Document.session`` (and surfaced
        through ``ConversationSession.documents``); we also fold in any CSVs
        linked through the session's messages' ``documents`` M2M so a file
        referenced on an individual turn is still reachable.
        """
        session_docs = conversation.documents.filter(
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            file_type="csv",
        )
        message_docs = Document.objects.filter(
            chat_messages__conversation=conversation,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            file_type="csv",
        )
        return (session_docs | message_docs).distinct()

    # Bounds for injected attachment text so a huge file cannot blow the token
    # budget: cap each document's text and the number of documents folded in.
    _MAX_ATTACHMENT_CHARS = 8000
    _MAX_ATTACHMENT_DOCS = 5

    def _chat_attachment_texts(self, conversation):
        """Return ``(title, text)`` for this session's non-CSV chat attachments.

        Chat attachments (``source=chat``) are never embedded in the vector
        store, so a question about an attached txt/pdf/docx/image is answered by
        reading the file directly and injecting its extracted text into the
        prompt. CSV attachments are EXCLUDED here — they drive the deterministic
        chart builder (``_build_csv_distribution_chart``) instead.

        Documents are reachable the same two ways as ``_chat_csv_documents``:
        the session (``ConversationSession.documents`` via ``Document.session``)
        and the session's messages (``Message.documents`` M2M). Each document's
        text is read via ``documents.services.extract_document_text`` (which
        OCRs images via tesseract), capped per document, and docs with no usable
        text are skipped. The number of docs folded in is bounded.
        """
        if conversation is None:
            return []

        session_docs = conversation.documents.filter(
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
        ).exclude(file_type="csv")
        message_docs = Document.objects.filter(
            chat_messages__conversation=conversation,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
        ).exclude(file_type="csv")
        documents = (session_docs | message_docs).distinct()

        sections = []
        for document in documents[: self._MAX_ATTACHMENT_DOCS]:
            try:
                text = document_services.extract_document_text(document)
            except Exception:  # a bad attachment must never break the answer
                logger.exception(
                    "Could not extract text from chat attachment %s", document.pk
                )
                continue
            if not text:
                continue
            sections.append((document.title, text[: self._MAX_ATTACHMENT_CHARS]))
        return sections

    def _build_csv_distribution_chart(self, conversation, question):
        if not self._is_chart_request(question):
            return None

        wants_pie = bool(re.search(r"\bpie\b", question, re.I))
        # The user's line/trend wording is only honoured when the summarized
        # column turns out to be numeric/continuous; see the type decision
        # after _summarize_csv_column below.
        wants_line = bool(
            re.search(r"\b(line|trend)\b", question, re.I)
            or re.search(r"over\s+time", question, re.I)
        )

        documents = self._chat_csv_documents(conversation)
        for document in documents:
            try:
                with document.file.open("rb") as source:
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
                        continue

                    rows = [
                        row
                        for row in reader
                        if row and any(cell.strip() for cell in row)
                    ]
            except (OSError, UnicodeDecodeError, csv.Error):
                logger.exception(
                    "Could not read CSV %s to generate a local chart",
                    document.pk,
                )
                continue

            if not rows:
                continue

            # "<value> by <category>" aggregation (e.g. "count by city"): group
            # the rows by a label column and SUM a numeric value column so the
            # chart shows the file's real metric per category (NYC=10, LA=5,
            # SF=3) rather than a frequency count. This fires only when the
            # phrasing and columns clearly express that mapping; otherwise we
            # fall through to the distribution summary below.
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
                    f"by {headers[label_index]} from {document.title}, "
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

            # Decide the chart type from the DATA SHAPE first, user intent
            # second (within the pie|bar|line Literal):
            #   * explicit pie/share/proportion request -> pie
            #     (but only when the values can form a valid pie);
            #   * a numeric/continuous column the user asked to see as a
            #     line/trend -> line;
            #   * everything else (categorical data, or no line intent) -> bar.
            # A "line" request over categorical data is overridden to bar so the
            # chart never misrepresents the data, and the sentence below names
            # the type that was actually drawn.
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
                f"from {document.title}, based on {total_records:,} records.",
                chart,
            )
        return None

    # Maximum distinct categories to keep a chart (and especially a pie) readable.
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

        # Columns the user explicitly named, in question order, as (match_pos,
        # index) so we can tell which was mentioned first.
        named = []
        for index, header in enumerate(headers):
            if not header:
                continue
            match = re.search(rf"\b{re.escape(header)}s?\b", question, re.I)
            if match:
                named.append((match.start(), index))
        if len(named) < 2:
            return None

        # Prefer an explicit "<X> by <Y>" phrasing: X is the value, Y the label.
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

        # The value column must be numeric across the data rows.
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
                return None  # value column is not numeric -> not an aggregation
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

            # Treat a column as numeric only when it is predominantly numbers
            # and still has few enough distinct values to chart meaningfully.
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

        # 1. Explicit column named in the question wins.
        if explicit_index is not None:
            summarized = summarize(explicit_index)
            if summarized and len(summarized[0]) >= 1:
                labels, is_numeric = summarized
                return explicit_index, labels[:max_categories], is_numeric

        # 2. Auto-select the most chart-friendly column.
        best = None  # (score, index, labels, is_numeric)
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
            # A single category makes a pointless pie/bar chart; skip it unless
            # it is the only thing available.
            if distinct < 2:
                score = -1
            elif distinct > max_categories:
                # Too many slices to be readable; strongly deprioritize.
                score = -distinct
            else:
                # Prefer categorical columns with a modest number of groups.
                # Fewer, well-populated groups score higher (ideal for pies).
                total = sum(count for _, count in labels)
                coverage = total / max(len(rows), 1)
                score = coverage * 100 - distinct
                # Prefer text categories over raw numeric spreads for pies.
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

    def _cached_answer(
        self,
        conversation,
        question,
        current_signature,
        exclude_user_message=None,
    ):
        """Return a prior assistant answer for an identical question in this
        conversation, or ``None``.

        Repeated queries are served from the database so the LLM is not called
        again. Matching is per-conversation and exact (case-insensitive, trimmed)
        to avoid surfacing another conversation's or user's answer. The cached
        answer reuses the stored content, citations, and chart_data.

        The cache is context-aware: a text match is only a HIT when the stored
        answer's ``context_signature`` is non-null AND equals
        ``current_signature`` (the document context at ask time). A changed
        signature (file uploaded / reprocessed / deleted / newly READY) or a
        legacy NULL signature is a MISS, so retrieval and the LLM run again.
        """
        if conversation is None:
            return None
        normalized = question.strip()
        if not normalized:
            return None

        user_turns = conversation.messages.filter(
            role=Message.ROLE_USER,
            content__iexact=normalized,
        )
        if exclude_user_message is not None:
            user_turns = user_turns.exclude(pk=exclude_user_message.pk)
        for user_turn in user_turns.order_by("created_at"):
            assistant = (
                conversation.messages.filter(
                    role=Message.ROLE_ASSISTANT,
                    created_at__gt=user_turn.created_at,
                )
                .order_by("created_at")
                .first()
            )
            if assistant is None:
                continue
            if (
                assistant.context_signature is not None
                and assistant.context_signature == current_signature
            ):
                return assistant
        return None

    def process_message(
        self,
        user,
        message_text,
        conversation=None,
        user_message=None,
        company=None,
        attachment_ids=None,
        model_name=None,
    ):
        """Run the full RAG pipeline for one user message, persisting Messages.

        Retrieves grounding chunks, builds a cited prompt, asks the LLM for a
        structured completion (with a deterministic CSV chart fallback for chart
        requests), and persists the user + assistant ``Message`` rows inside one
        transaction. Repeated identical questions with an unchanged document
        context are served from the per-session cache without calling the LLM.
        """
        if not user or not user.is_authenticated:
            raise ValueError("A signed-in user is required to chat.")

        if conversation and (
            conversation.user_id != user.pk
            or conversation.company_id != getattr(company, "pk", None)
        ):
            raise ValueError("Conversation does not belong to the signed-in user.")

        assistant_model_name = model_name or _DEFAULT_MODEL_NAME

        # Document-context signature at ask time. Computed once so the same
        # value feeds the cache decision and, on a miss, the stored answer.
        current_signature = self._context_signature(user, company)

        # Repeated-query cache: if this exact question already has an answer in
        # this conversation AND the document context is unchanged, serve it from
        # the database without calling the LLM. A changed context is a miss.
        cached = self._cached_answer(
            conversation,
            message_text,
            current_signature,
            exclude_user_message=user_message,
        )
        if cached is not None:
            with transaction.atomic():
                if not user_message:
                    user_message = self.create_message(
                        conversation,
                        Message.ROLE_USER,
                        message_text,
                        model_name="user",
                        actor=user,
                    )
                self._link_attachments(user_message, conversation, attachment_ids)
                assistant_message = self.create_message(
                    conversation,
                    Message.ROLE_ASSISTANT,
                    cached.content,
                    model_name=cached.model_name,
                    citations=cached.citations,
                    chart_data=cached.chart_data,
                    context_signature=cached.context_signature,
                    actor=user,
                )
                conversation.save(
                    actor=user, update_fields=["updated_at", "updated_by"]
                )
            return assistant_message

        history_query = (
            conversation.messages.exclude(role=Message.ROLE_SYSTEM)
            if conversation
            else Message.objects.none()
        )
        if user_message:
            history_query = history_query.exclude(pk=user_message.pk)
        # Give the LLM a reference to the whole chat (backed by Postgres): the
        # most recent turns verbatim, plus a condensed digest of everything
        # earlier so long conversations keep their context without exceeding
        # the token budget.
        all_messages = list(history_query.order_by("created_at"))
        history = self._build_conversation_context(all_messages)
        try:
            chunks = self._find_relevant_chunks(
                user,
                message_text,
                conversation,
                company,
            )
            # Pre-extract the text of any non-CSV chat attachments on this
            # session so the answer is grounded in the attached file (text/pdf/
            # docx/image-OCR), not general knowledge. CSVs are handled by the
            # deterministic chart builder below, so they are excluded here.
            attachment_sections = self._chat_attachment_texts(conversation)
            prompt, citations = self._build_prompt(
                history,
                message_text,
                chunks,
                attachment_sections=attachment_sections,
            )

            completion = None
            provider_error = None
            try:
                completion = self._llm.generate_structured(prompt, ChatCompletion)
                # Defensive strip (LLM answer path only): remove any chart-spec
                # JSON the model leaked into the prose so the user never sees a
                # raw chart dump. The deterministic CSV fallback below builds its
                # own prose and is intentionally not run through this.
                sanitized = self._strip_chart_json_blocks(completion.response)
                if not sanitized.strip():
                    # Stripping emptied the prose (model returned only a JSON
                    # dump): fall back to a short human sentence so the empty
                    # response guard is not tripped and the chart still renders.
                    sanitized = (
                        "Here is the chart you requested."
                        if completion.chart is not None
                        else "I could not produce a chart for that request."
                    )
                if sanitized != completion.response:
                    completion = completion.model_copy(update={"response": sanitized})
            except (ChatRetrievalError, ChatProviderError):
                raise
            except Exception as exc:  # provider unreachable / bad key / transient
                provider_error = exc
                logger.warning(
                    "LLM provider failed to generate a response: %s", exc
                )

            # Deterministic CSV chart. For a chart request, build the chart from
            # the ACTUAL rows of a chat-attached CSV. When such a CSV exists the
            # locally-built chart is AUTHORITATIVE and OVERRIDES any chart the
            # LLM returned, so the chart always reflects the file's real values
            # (NYC=10, LA=5, SF=3) instead of fabricated general knowledge. This
            # also works with no key. When there is NO chat CSV we keep the old
            # behaviour: fall back to the (now-empty) builder only when the LLM
            # returned no chart, otherwise trust the LLM's chart.
            if conversation and self._is_chart_request(message_text):
                local_chart = self._build_csv_distribution_chart(
                    conversation, message_text
                )
                needs_fallback = completion is None or completion.chart is None
                if local_chart and (needs_fallback or completion.chart is not None):
                    # Text and chart come from one source so they can never
                    # contradict: when we attach the locally-built chart we also
                    # use the builder's own sentence describing it, discarding
                    # the LLM's prose/chart (which, for an attached CSV, is
                    # fabricated general knowledge or a refusal that would
                    # otherwise sit next to a correct rendered chart).
                    response_text, chart = local_chart
                    completion = ChatCompletion(
                        response=response_text, chart=chart
                    )

            # If the provider failed and no fallback produced an answer, surface
            # the provider error to the caller.
            if completion is None:
                raise ChatProviderError(
                    "The LLM provider failed to generate a response."
                ) from provider_error
        except (ChatRetrievalError, ChatProviderError):
            raise
        except Exception as exc:
            raise ChatProviderError(
                "The LLM provider failed to generate a response."
            ) from exc

        if not completion.response.strip():
            raise ChatProviderError("The LLM provider returned an empty response.")

        with transaction.atomic():
            convo = conversation or self._create_session(user, message_text, company)
            if user_message:
                if user_message.conversation_id != convo.pk:
                    raise ValueError(
                        "User message does not belong to this conversation."
                    )
            else:
                user_message = self.create_message(
                    convo,
                    Message.ROLE_USER,
                    message_text,
                    model_name="user",
                    actor=user,
                )
            self._link_attachments(user_message, convo, attachment_ids)
            assistant_message = self.create_message(
                convo,
                Message.ROLE_ASSISTANT,
                completion.response.strip(),
                model_name=assistant_model_name,
                citations=citations,
                chart_data=(
                    completion.chart.model_dump(mode="json")
                    if completion.chart
                    else None
                ),
                context_signature=current_signature,
                actor=user,
            )
            # Title the session from its first message when it is still the
            # default placeholder (first ~50 chars). The retitle-or-audit-touch
            # (which ALWAYS saves the session) lives in the shared helper so the
            # graph path reuses the identical semantics.
            retitle_if_default(
                convo,
                llm_title=completion.title,
                fallback_text=message_text,
                actor=user,
            )
        return assistant_message

    def _create_session(self, user, message_text, company):
        """Create a new ConversationSession to hold a fresh message thread."""
        session = ConversationSession(
            company=company,
            user=user,
            title="New session",
        )
        session.save(actor=user)
        return session

    def _link_attachments(self, user_message, conversation, attachment_ids):
        """Attach the given chat documents to the user message (and session).

        Attachments are scoped to the conversation's company so one user can
        never link another company's document onto a message. The documents are
        linked both to the message (``Message.documents`` M2M) and the session
        (via ``Document.session``) so the CSV chart builder and future turns can
        find them.
        """
        if not attachment_ids:
            return
        documents = list(
            Document.objects.filter(
                pk__in=attachment_ids,
                company=conversation.company,
            )
        )
        if not documents:
            return
        user_message.documents.add(*documents)
        for document in documents:
            if document.session_id != conversation.pk:
                document.session = conversation
                document.save(update_fields=["session", "updated_at"])
