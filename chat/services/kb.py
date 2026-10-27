from __future__ import annotations

import logging
import re

from documents.models import Document

logger = logging.getLogger(__name__)

CHAT_KB_BACKEND = "temporal"

# Pending-state key on ConversationSession.metadata where the numbered
# add-to-KB selection is remembered between turns.
PENDING_KB_KEY = "pending_kb_selection"


# --------------------------------------------------------------------------- #
# Intent detection (shared with the view-level net)
# --------------------------------------------------------------------------- #
_ADD_TO_KB_RE = re.compile(
    r"\b(add|save|store|upload|put|index|ingest)\b.*\b"
    r"(knowledge\s*base|knowledgebase|kb|document\s*library|library|documents?|"
    r"company\s*(docs|documents|knowledge))\b"
    # Also trigger on an explicit "process this document/file" command.
    r"|\bprocess\b.*\b(document|doc|file|attachment)s?\b",
    re.IGNORECASE,
)

_STATUS_RE = re.compile(
    r"\b(status|progress|processing|how\s+many|which)\b.*"
    r"\b(document|doc|file|attachment|task|job|embed|chunk|process)\w*\b"
    r"|\b(document|doc|file|task|job)\w*\b.*\b(status|progress|processing|done|"
    r"ready|finished|complete|pending|queued)\b"
    r"|\bwhat('?s| is)\b.*\b(processing|in\s+progress|being\s+processed)\b"
    # "show me the status", "show status", "check the status", "get status" —
    # a bare status request with explicit show/check/get wording (no doc token
    # required). Bounded to show/check/get so ordinary questions like "what is
    # the status of the French economy" do NOT match.
    r"|\b(show|check|get)\b.*\bstatus\b"
    # "processing status", "embedding status", "indexing status".
    r"|\b(processing|embedding|indexing)\s+status\b"
    # "is my file done/ready/finished/processed/embedded?"
    r"|\bis\s+my\s+\w+\s+(done|ready|finished|complete|completed|processed|"
    r"embedded|indexed)\b",
    re.IGNORECASE,
)


def is_add_to_kb_request(text: str | None) -> bool:
    """True when the message asks to add/process the attached file into the KB.

    ``_ADD_TO_KB_RE`` already matches the required phrasings ("add the document
    to the company documents", "add documents to the knowledge base",
    "process this file", ...), so it is kept as-is. NOTE: the pending-selection
    follow-up handler in ``views.send()`` runs BEFORE this check, so an answer
    like "all" / "second one" while a selection is pending is treated as the
    selection, not a brand-new add request.
    """
    if not text:
        return False
    return bool(_ADD_TO_KB_RE.search(text))


def is_status_request(text: str | None) -> bool:
    """True when the message asks about document processing status/progress."""
    if not text:
        return False
    return bool(_STATUS_RE.search(text))


# --------------------------------------------------------------------------- #
# Add attachments to the knowledge base (Temporal)
# --------------------------------------------------------------------------- #
def _is_already_embedded(document: Document) -> bool:
    """Dedup rule: already a library doc, ready, with chunks -> skip reprocess."""
    return (
        document.source == Document.SOURCE_LIBRARY
        and document.status == Document.STATUS_READY
        and (document.chunk_count or 0) > 0
    )


def _find_existing_kb_document(company, title: str) -> Document | None:
    """Find an already-processed document in the KB with the same title.
    
    Matches are case-sensitive and title-exact. If a document with the same
    filename but different content already exists in the KB, this check will
    skip reprocessing the new upload. This is an acceptable limitation:
    true content deduplication would require file hashing, which is beyond
    the scope of this fix.
    
    Args:
        company: The Company to search within (multi-tenant scoping)
        title: The document title (filename) to match
        
    Returns:
        The first matching Document if found, None otherwise
    """
    return Document.objects.filter(
        company=company,
        source=Document.SOURCE_LIBRARY,
        status=Document.STATUS_READY,
        title=title,
    ).exclude(chunk_count=0).first()


def addable_documents(*, conversation, company, attachment_ids=None) -> list:
    """Return the de-duplicated, ordered list of files eligible to add to the KB.

    Start from explicit ``attachment_ids`` (scoped to ``company``) when given,
    else the session's chat+library attachments. Exclude anything already
    embedded (``_is_already_embedded``) or already in the KB by title
    (``_find_existing_kb_document``), and collapse duplicate uploads of the SAME
    file (identical title + size). The result is sorted by ``(title, id)`` so
    the numbered list shown to the user and the saved ``files`` order always
    match.
    """
    ids = list(attachment_ids or [])
    if ids:
        documents = list(Document.objects.filter(pk__in=ids, company=company))
    elif conversation is not None:
        documents = list(
            conversation.documents.filter(
                source__in=[Document.SOURCE_CHAT, Document.SOURCE_LIBRARY],
                company=company,
            )
        )
    else:
        documents = []

    # Drop files already embedded / already in the KB by title.
    eligible = []
    for doc in documents:
        if _is_already_embedded(doc):
            continue
        if _find_existing_kb_document(company, doc.title) is not None:
            continue
        eligible.append(doc)

    # Collapse duplicate uploads of the SAME file (identical title + size).
    seen, unique_documents = set(), []
    for doc in eligible:
        key = ((doc.title or "").strip().casefold(), doc.file_size)
        if key in seen:
            continue
        seen.add(key)
        unique_documents.append(doc)

    # Stable order so the numbered question and saved state always agree.
    unique_documents.sort(key=lambda d: ((d.title or "").casefold(), str(d.id)))
    return unique_documents


# --------------------------------------------------------------------------- #
# Pending-selection state (stored on ConversationSession.metadata)
# --------------------------------------------------------------------------- #
def pending_selection_state(files) -> dict:
    """Build the pending-selection payload for the numbered ``files`` list."""
    from django.utils import timezone

    return {
        "files": [{"id": str(d.id), "title": d.title} for d in files],
        "created_at": timezone.now().isoformat(),
    }


def _title_of(f) -> str:
    """Title from either a Document instance or a saved ``{"title"}`` dict."""
    return f["title"] if isinstance(f, dict) else f.title


def build_numbered_question(files) -> str:
    """A 1..N numbered list of ``files`` plus the "which file(s)?" prompt.

    ``files`` may be Document instances (first ask) or saved ``{"id","title"}``
    dicts (the re-ask path reads them back from metadata).
    """
    numbered = "\n".join(
        f"{i}. {_title_of(d)}" for i, d in enumerate(files, start=1)
    )
    return (
        f"I found {len(files)} files attached to this chat:\n\n"
        f"{numbered}\n\n"
        "Which file(s) would you like to add to the knowledge base? You can say "
        "'all', a number (e.g. 2), a range like 'first two', or a filename."
    )


def save_pending_selection(*, conversation, files, actor) -> dict:
    """Persist the numbered pending selection on the session metadata."""
    state = pending_selection_state(files)
    conversation.metadata = {**(conversation.metadata or {}), PENDING_KB_KEY: state}
    conversation.save(
        actor=actor, update_fields=["metadata", "updated_at", "updated_by"]
    )
    return state


def clear_pending_selection(*, conversation, actor) -> None:
    """Remove the pending selection from the session metadata (if any)."""
    metadata = dict(conversation.metadata or {})
    if PENDING_KB_KEY in metadata:
        metadata.pop(PENDING_KB_KEY, None)
        conversation.metadata = metadata
        conversation.save(
            actor=actor, update_fields=["metadata", "updated_at", "updated_by"]
        )


def workflow_id_for(document_id) -> str:
    """Temporal workflow id convention for a document (``doc-<id>``)."""
    return f"doc-{document_id}"


def temporal_ui_url(workflow_id: str) -> str:
    """Deep link to a workflow in the Temporal Web UI.

    Base comes from ``settings.TEMPORAL_UI_URL`` (default http://localhost:8081).
    The default namespace is ``default``; the workflows view accepts the
    workflow id path segment.
    """
    from django.conf import settings

    base = (getattr(settings, "TEMPORAL_UI_URL", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/namespaces/default/workflows/{workflow_id}"


def add_session_attachments_to_kb(
    *, conversation, company, attachment_ids, actor
) -> dict:
    """Promote the turn's chat attachments to library docs and dispatch Temporal.

    Returns a dict with:
      * ``content``   — the user-facing assistant reply.
      * ``queued``    — list of ``{id, title, workflow_id}`` dispatched to Temporal.
      * ``skipped``   — titles already embedded (deduped).
      * ``failed_ids``— document ids whose dispatch failed (retryable).
    """
    ids = list(attachment_ids or [])

    # Single source of truth for the addable set (dedup + exclusions + order).
    documents = addable_documents(
        conversation=conversation, company=company, attachment_ids=ids
    )

    # When multiple distinct session files exist and no explicit IDs were passed,
    # ask the user which file(s) to add and REMEMBER that we asked (pending
    # state) so the next message is interpreted as the selection answer.
    if not ids and len(documents) > 1:
        save_pending_selection(
            conversation=conversation, files=documents, actor=actor
        )
        return {
            "content": build_numbered_question(documents),
            "queued": [],
            "skipped": [],
            "failed_ids": [],
            "pending": True,
        }

    if not documents:
        return {
            "content": (
                "I don't see a file on this chat to add. Attach a document and "
                "ask me again to add it to the company knowledge base."
            ),
            "queued": [],
            "skipped": [],
            "failed_ids": [],
        }

    queued, skipped, failed_ids = _dispatch_documents(documents, company, actor)
    return {
        "content": _compose_kb_reply(queued, skipped, failed_ids),
        "queued": queued,
        "skipped": skipped,
        "failed_ids": failed_ids,
    }


def _dispatch_documents(documents, company, actor):
    """Reset + dispatch each document through Temporal; skip already-embedded.

    Returns ``(queued, skipped, failed_ids)`` using the same shape the KB reply
    composer expects. Shared by ``add_session_attachments_to_kb`` and
    ``dispatch_selected`` so there is exactly one dispatch loop.
    """
    from documents.tasks import dispatch_document_processing

    queued, skipped, failed_ids = [], [], []
    for document in documents:
        if _is_already_embedded(document):
            skipped.append(document.title)
            continue

        # Check if another document with same title exists in KB
        existing = _find_existing_kb_document(company, document.title)
        if existing:
            skipped.append(document.title)
            continue

        # Reset to a clean state before (re)processing so the backend starts fresh.
        document.source = Document.SOURCE_LIBRARY
        document.status = Document.STATUS_UPLOADED
        document.processing_stage = ""
        document.processing_progress = 0
        document.processing_error = ""
        document.chunk_count = 0
        document.save(
            actor=actor,
            update_fields=[
                "source",
                "status",
                "processing_stage",
                "processing_progress",
                "processing_error",
                "chunk_count",
                "updated_at",
            ],
        )

        try:
            # CHAT path: always Temporal. dispatch_document_processing flips the
            # row to QUEUED and returns (job_id, backend); job_id == doc-<id>.
            job_id, _backend = dispatch_document_processing(
                document.id, backend=CHAT_KB_BACKEND
            )
            wf = job_id or workflow_id_for(document.id)
            queued.append(
                {
                    "id": str(document.id),
                    "title": document.title,
                    "workflow_id": wf,
                    "temporal_url": temporal_ui_url(wf),
                }
            )
        except Exception:  # noqa: BLE001 - surface a retryable per-file failure
            logger.exception(
                "Temporal dispatch failed for document %s", document.pk
            )
            Document.objects.filter(pk=document.pk).update(
                status=Document.STATUS_FAILED,
                processing_stage="failed",
                processing_error=(
                    "Could not start processing. This is usually a temporary "
                    "connectivity issue — please retry."
                ),
            )
            failed_ids.append(str(document.id))

    return queued, skipped, failed_ids


def _compose_kb_reply(queued, skipped, failed_ids) -> str:
    parts: list[str] = []
    if queued:
        names = ", ".join(q["title"] for q in queued)
        ids = ", ".join(q["workflow_id"] for q in queued)
        parts.append(
            f"Added {len(queued)} file(s) to the company knowledge base: {names}. "
            f"Processing started on Temporal (workflow id: {ids}). Text "
            "extraction, chunking, and embedding run in the background — you can "
            "track progress under Documents, or ask me for the status any time."
        )
    if skipped:
        parts.append(
            f"Skipped {len(skipped)} file(s) already in the knowledge base: "
            f"{', '.join(skipped)}."
        )
    if failed_ids:
        parts.append(
            "I couldn't start processing for "
            f"{len(failed_ids)} file(s) right now — this is usually a temporary "
            "connectivity issue. Please try again in a moment using the Retry "
            "button."
        )
    if not parts:
        parts.append(
            "I couldn't add the attached file(s) to the knowledge base. Please "
            "try again, or check that the file uploaded correctly."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Selection resolution (deterministic parser) + dispatch of a chosen subset
# --------------------------------------------------------------------------- #
_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
_ALL_WORDS = {"all", "both", "everything", "every", "all of them", "each", "each one"}
_CANCEL_WORDS = {
    "cancel",
    "never mind",
    "nevermind",
    "stop",
    "forget it",
    "no",
    "nothing",
    "none",
}


def _int_from_word(token: str) -> int | None:
    """Map a bare/word/ordinal numeric token to its 1-based integer (or None)."""
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    if token in _WORD_NUMBERS:
        return _WORD_NUMBERS[token]
    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token]
    # Digit ordinals: "2nd", "3rd", "1st", "4th".
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", token)
    if m:
        return int(m.group(1))
    return None


def ids_for_indices(files, indices) -> list:
    """Map 1-based ``indices`` into ``files`` to their document ids (in order)."""
    ids = []
    for idx in indices:
        if isinstance(idx, int) and 1 <= idx <= len(files):
            ids.append(files[idx - 1]["id"])
    # De-dup while preserving order.
    seen, unique = set(), []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        unique.append(i)
    return unique


def resolve_selection(answer: str, files: list) -> dict:
    """Map a free-text selection ``answer`` to a subset of the numbered ``files``.

    Deterministic — no LLM. Handles, case-insensitively, over the exact numbered
    ``files`` list (each a ``{"id", "title"}`` dict in displayed order):

    * ``all`` / ``both`` / ``everything`` / ``every file`` -> every file.
    * ``cancel`` / ``never mind`` / ``stop`` / ``forget it`` -> cancel.
    * bare/compound numbers: "2", "1 and 3", "1,3", "files 1 & 2".
    * word-numbers and ordinals (one..ten): "second one", "the 2nd",
      "number two", "file two".
    * ``first`` / ``last`` (single) and ``first N`` / ``last N`` /
      "first two" / "last three" (word or digit N).
    * a filename or a case-insensitive substring match against titles (and the
      stem without extension).

    Out-of-range numbers are ignored. When nothing resolves, returns
    ``action="none"`` with an empty id list. Returns
    ``{"ids", "action", "matched"}`` where ``action`` is one of
    ``"all" | "subset" | "cancel" | "none"``.
    """
    text = (answer or "").strip().lower()
    if not text:
        return {"ids": [], "action": "none", "matched": []}

    def _result(indices, action):
        ids = ids_for_indices(files, indices)
        matched = [
            files[i - 1]["title"] for i in indices if 1 <= i <= len(files)
        ]
        return {"ids": ids, "action": action, "matched": matched}

    # Cancel takes precedence over everything else.
    if text in _CANCEL_WORDS or any(
        re.search(rf"\b{re.escape(w)}\b", text) for w in ("never mind", "forget it")
    ) or text in {"cancel", "stop"}:
        return {"ids": [], "action": "cancel", "matched": []}

    # "all" / "both" / "everything" / "every file" / "each one".
    if (
        text in _ALL_WORDS
        or re.search(r"\b(all|both|everything|every\s+file|each\s+one)\b", text)
    ):
        return _result(list(range(1, len(files) + 1)), "all")

    # first N / last N (word or digit N), and bare "first"/"last".
    m = re.search(r"\b(first|last)\b(?:\s+(\w+))?", text)
    if m:
        direction = m.group(1)
        n_token = m.group(2)
        n = _int_from_word(n_token) if n_token else 1
        # "first one"/"last one" -> N=1 (map 'one' which _int_from_word gives 1).
        if n is None:
            n = 1
        n = max(0, min(n, len(files)))
        if n:
            if direction == "first":
                return _result(list(range(1, n + 1)), "subset")
            return _result(list(range(len(files) - n + 1, len(files) + 1)), "subset")

    # Explicit numbers / ordinals anywhere in the answer:
    #   "2", "1 and 3", "1,3", "files 1 & 2", "number two", "the 2nd".
    # NOTE: a bare word "one" is ambiguous — in "second one" / "the first one"
    # it is a NOUN, not the number 1. We only count "one" as the number when it
    # does not directly follow another number/ordinal token.
    tokens = re.findall(r"\d+(?:st|nd|rd|th)?|[a-z]+", text)
    nums = []
    prev_was_numeric = False
    for tok in tokens:
        val = _int_from_word(tok)
        is_numeric_token = val is not None
        if tok == "one" and prev_was_numeric:
            # "second one" / "the 2nd one" — 'one' is the noun, skip it.
            prev_was_numeric = False
            continue
        if val is not None and 1 <= val <= len(files):
            nums.append(val)
        prev_was_numeric = is_numeric_token
    if nums:
        # Preserve order, de-dup.
        seen, ordered = set(), []
        for n in nums:
            if n in seen:
                continue
            seen.add(n)
            ordered.append(n)
        return _result(ordered, "subset")

    # Filename / substring match against titles (and stems).
    matched_indices = []
    for i, f in enumerate(files, start=1):
        title = (f.get("title") or "").lower()
        stem = title.rsplit(".", 1)[0]
        if title and (text in title or (stem and text in stem) or title in text):
            matched_indices.append(i)
    if matched_indices:
        return _result(matched_indices, "subset")

    return {"ids": [], "action": "none", "matched": []}


def dispatch_selected(*, conversation, company, ids, actor) -> dict:
    """Dispatch the chosen document ``ids`` (in order) via Temporal + reply.

    Loads the documents scoped to ``company``, preserving the given id order,
    runs the shared dispatch loop, and returns the same
    ``{content, queued, skipped, failed_ids}`` shape as
    ``add_session_attachments_to_kb``.
    """
    id_list = [str(i) for i in (ids or [])]
    by_id = {
        str(d.id): d
        for d in Document.objects.filter(pk__in=id_list, company=company)
    }
    documents = [by_id[i] for i in id_list if i in by_id]

    if not documents:
        return {
            "content": (
                "I couldn't match that to any of the attached files. Please try "
                "again with a number, a filename, or 'all'."
            ),
            "queued": [],
            "skipped": [],
            "failed_ids": [],
        }

    queued, skipped, failed_ids = _dispatch_documents(documents, company, actor)
    return {
        "content": _compose_kb_reply(queued, skipped, failed_ids),
        "queued": queued,
        "skipped": skipped,
        "failed_ids": failed_ids,
    }


def handle_pending_selection(
    *, conversation, company, message_text, actor
) -> dict | None:
    """Interpret ``message_text`` as the answer to a pending KB selection.

    Returns ``None`` when there is no pending selection (caller proceeds with
    the normal router/LLM). Otherwise returns a result dict:

    * cancel -> clears pending state, ``{"cleared": True, ...}``.
    * unrecognized/empty -> KEEPS state, ``{"reask": True, ...}`` with a short
      clarification + the numbered list (re-asks ONCE, never silently loops).
    * resolved -> dispatches the chosen file(s) via Temporal, CLEARS state, and
      returns the ``dispatch_selected`` result dict.

    This is the single entry point used by BOTH the ``views.send()`` seam and the
    ``add_to_knowledge_base`` tool, so the two orchestration paths behave the
    same.
    """
    state = (conversation.metadata or {}).get(PENDING_KB_KEY)
    if not state:
        return None

    files = list(state.get("files") or [])
    resolution = resolve_selection(message_text or "", files)
    action = resolution["action"]

    if action == "cancel":
        clear_pending_selection(conversation=conversation, actor=actor)
        return {
            "content": "Okay, I won't add anything to the knowledge base.",
            "queued": [],
            "skipped": [],
            "failed_ids": [],
            "cleared": True,
        }

    if action == "none" or not resolution["ids"]:
        # Keep the pending state and re-ask with a short clarification.
        return {
            "content": (
                "Sorry, I didn't catch which file(s) you meant. "
                + build_numbered_question(files)
            ),
            "queued": [],
            "skipped": [],
            "failed_ids": [],
            "reask": True,
        }

    result = dispatch_selected(
        conversation=conversation,
        company=company,
        ids=resolution["ids"],
        actor=actor,
    )
    clear_pending_selection(conversation=conversation, actor=actor)
    return result


def retry_document_processing(*, document_id, company, actor) -> dict:
    """Re-dispatch a previously failed chat KB document through Temporal.

    Returns ``{"ok": bool, "content": str, "workflow_id": str | None,
    "failed": bool}``. Scoped to ``company`` so a user cannot retry another
    company's document.
    """
    from documents.tasks import dispatch_document_processing

    document = Document.objects.filter(pk=document_id, company=company).first()
    if document is None:
        return {
            "ok": False,
            "content": "That document is not available.",
            "workflow_id": None,
            "failed": True,
        }

    if _is_already_embedded(document):
        return {
            "ok": True,
            "content": f"“{document.title}” is already in the knowledge base.",
            "workflow_id": workflow_id_for(document.id),
            "failed": False,
        }

    document.source = Document.SOURCE_LIBRARY
    document.status = Document.STATUS_UPLOADED
    document.processing_stage = ""
    document.processing_progress = 0
    document.processing_error = ""
    document.chunk_count = 0
    document.save(
        actor=actor,
        update_fields=[
            "source",
            "status",
            "processing_stage",
            "processing_progress",
            "processing_error",
            "chunk_count",
            "updated_at",
        ],
    )

    try:
        job_id, _backend = dispatch_document_processing(
            document.id, backend=CHAT_KB_BACKEND
        )
    except Exception:  # noqa: BLE001
        logger.exception("Temporal retry dispatch failed for %s", document.pk)
        Document.objects.filter(pk=document.pk).update(
            status=Document.STATUS_FAILED,
            processing_stage="failed",
            processing_error=(
                "Could not start processing. Please retry in a moment."
            ),
        )
        return {
            "ok": False,
            "content": (
                "Still couldn't start processing — please try again shortly."
            ),
            "workflow_id": None,
            "failed": True,
        }

    wf = job_id or workflow_id_for(document.id)
    return {
        "ok": True,
        "content": (
            f"Restarted processing for “{document.title}” on Temporal "
            f"(workflow id: {wf})."
        ),
        "workflow_id": wf,
        "failed": False,
    }


# --------------------------------------------------------------------------- #
# Document processing status
# --------------------------------------------------------------------------- #
_IN_FLIGHT = (Document.STATUS_QUEUED, Document.STATUS_PROCESSING)

# A doc/task id is a 26-char ULID, optionally prefixed with the Temporal
# "doc-" workflow prefix.
_ID_RE = re.compile(r"\b(?:doc-)?([0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26})\b")


def _extract_doc_id(text: str | None) -> str | None:
    if not text:
        return None
    match = _ID_RE.search(text)
    return match.group(1) if match else None


def _describe(document: Document) -> str:
    wf = workflow_id_for(document.id)
    progress = document.processing_progress_display
    stage = document.processing_stage or "—"
    line = (
        f"- {document.title}: {document.get_status_display()} "
        f"({progress}%, stage: {stage}, backend: {document.processing_backend}, "
        f"workflow: {wf})"
    )
    if document.status == Document.STATUS_FAILED and document.processing_error:
        line += f" — {document.processing_error.strip()[:200]}"
    return line


def _status_documents(*, company, message_text: str | None):
    """Resolve the documents a status request refers to.

    Returns ``(documents, specific)``: when the message names a doc/task id,
    ``documents`` is that single doc (or empty) and ``specific`` is True;
    otherwise ``documents`` is the company's in-flight docs plus a short tail of
    recently finished ones, and ``specific`` is False.
    """
    doc_id = _extract_doc_id(message_text)
    if doc_id:
        document = Document.objects.filter(pk=doc_id, company=company).first()
        return ([document] if document else []), True

    in_flight = list(
        Document.objects.filter(company=company, status__in=_IN_FLIGHT).order_by(
            "-updated_at"
        )[:25]
    )
    recent = list(
        Document.objects.filter(
            company=company,
            status__in=(Document.STATUS_READY, Document.STATUS_FAILED),
        ).order_by("-updated_at")[:5]
    )
    return in_flight + recent, False


def document_status_report(*, company, message_text: str | None) -> str:
    """Build a readable status report for the company's documents.

    If the message names a specific document/task id, report just that one;
    otherwise list in-flight docs (queued/processing) plus a short tail of the
    most recently finished (ready/failed) ones.
    """
    if company is None:
        return "I couldn't determine the company for this chat."

    doc_id = _extract_doc_id(message_text)
    if doc_id:
        document = Document.objects.filter(pk=doc_id, company=company).first()
        if document is None:
            return (
                f"I couldn't find a document with id {doc_id} in this company. "
                "Double-check the id, or ask me to list what's processing."
            )
        return "Here is the status for that document:\n" + _describe(document)

    in_flight = list(
        Document.objects.filter(company=company, status__in=_IN_FLIGHT).order_by(
            "-updated_at"
        )[:25]
    )
    recent = list(
        Document.objects.filter(
            company=company,
            status__in=(Document.STATUS_READY, Document.STATUS_FAILED),
        ).order_by("-updated_at")[:5]
    )

    if not in_flight and not recent:
        return "No documents are being processed right now for this company."

    parts: list[str] = []
    if in_flight:
        parts.append(
            f"{len(in_flight)} document(s) currently in process:\n"
            + "\n".join(_describe(d) for d in in_flight)
        )
    else:
        parts.append("No documents are currently queued or processing.")
    if recent:
        parts.append(
            "Most recently finished:\n" + "\n".join(_describe(d) for d in recent)
        )
    parts.append(
        "You can watch the live progress below; ask again, or give me a "
        "specific document id, any time."
    )
    return "\n\n".join(parts)


def document_status_rows(*, company, document_ids=None, message_text=None) -> list:
    """Structured status rows for the live tracker (and the status endpoint).

    * ``document_ids`` — restrict to these ids (what a reply polls), else
    * ``message_text`` — resolve the same docs the prose report describes, else
    * all in-flight docs for the company.

    Each row: ``{id, workflow_id, title, status, status_display, stage,
    progress, backend, error, terminal}``. ``terminal`` is True for ready/failed
    so the client can stop polling.
    """
    if company is None:
        return []

    if document_ids:
        documents = list(
            Document.objects.filter(pk__in=list(document_ids), company=company)
        )
    elif message_text is not None:
        documents, _specific = _status_documents(
            company=company, message_text=message_text
        )
    else:
        documents = list(
            Document.objects.filter(company=company, status__in=_IN_FLIGHT).order_by(
                "-updated_at"
            )[:25]
        )

    rows = []
    for d in documents:
        if d is None:
            continue
        terminal = d.status in (Document.STATUS_READY, Document.STATUS_FAILED)
        wf = workflow_id_for(d.id)
        rows.append(
            {
                "id": str(d.id),
                "workflow_id": wf,
                "temporal_url": (
                    temporal_ui_url(wf) if d.processing_backend == "temporal" else ""
                ),
                "title": d.title,
                "status": d.status,
                "status_display": d.get_status_display(),
                "stage": d.processing_stage or "",
                "progress": d.processing_progress_display,
                "backend": d.processing_backend,
                "error": (d.processing_error or "").strip()[:300],
                "terminal": terminal,
            }
        )
    return rows


def status_document_ids(*, company, message_text: str | None) -> list:
    """Ids of the documents a status reply should live-track (non-terminal)."""
    if company is None:
        return []
    documents, _specific = _status_documents(
        company=company, message_text=message_text
    )
    return [
        str(d.id)
        for d in documents
        if d is not None and d.status in _IN_FLIGHT
    ]
