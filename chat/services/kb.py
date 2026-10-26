from __future__ import annotations

import logging
import re

from documents.models import Document

logger = logging.getLogger(__name__)

CHAT_KB_BACKEND = "temporal"


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
    r"|\bwhat('?s| is)\b.*\b(processing|in\s+progress|being\s+processed)\b",
    re.IGNORECASE,
)


def is_add_to_kb_request(text: str | None) -> bool:
    """True when the message asks to add/process the attached file into the KB."""
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
    """Find an already-processed document in the KB with the same title."""
    return Document.objects.filter(
        company=company,
        source=Document.SOURCE_LIBRARY,
        status=Document.STATUS_READY,
        title=title,
    ).exclude(chunk_count=0).first()


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
    from documents.tasks import dispatch_document_processing

    ids = list(attachment_ids or [])
    documents = list(
        Document.objects.filter(pk__in=ids, company=company)
    ) if ids else []

    # Fall back to the session's chat attachments if none were passed for the turn.
    if not documents and conversation is not None:
        documents = list(
            conversation.documents.filter(
                source__in=[Document.SOURCE_CHAT, Document.SOURCE_LIBRARY],
                company=company,
            )
        )

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

    return {
        "content": _compose_kb_reply(queued, skipped, failed_ids),
        "queued": queued,
        "skipped": skipped,
        "failed_ids": failed_ids,
    }


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
