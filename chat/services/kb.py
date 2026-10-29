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
    "process this file", ...), so it is kept as-is. Chat file commands use the
    narrower rules in ``chat.services.file_commands.classify`` instead.
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
# Temporal links (also imported by documents/admin.py)
# --------------------------------------------------------------------------- #
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


def retry_document_processing(*, document_id, company, actor) -> dict:
    """Back the legacy chat Retry button (``<sid>/reprocess/``).

    The only owner of the status mapping (review-3 F9): READY -> already in the
    knowledge base; QUEUED/PROCESSING -> already processing (conflict); FAILED
    -> ``controls.resume_document``; UPLOADED -> ``controls.start_document``.
    The controls lock the row, so a double click enqueues once.

    Returns ``{"ok", "content", "workflow_id", "conflict"}``. ``conflict`` is
    True when the document's state (or its absence) disallows the retry.
    """
    from documents import controls

    document = Document.objects.filter(pk=document_id, company=company).first()
    if document is None:
        return {
            "ok": False,
            "content": "That document is not available.",
            "workflow_id": None,
            "conflict": True,
        }

    wf = workflow_id_for(document.id)
    if document.status == Document.STATUS_READY:
        return {
            "ok": True,
            "content": f"“{document.title}” is already in the knowledge base.",
            "workflow_id": wf,
            "conflict": False,
        }
    if document.status in _IN_FLIGHT:
        return {
            "ok": False,
            "content": f"“{document.title}” is already processing.",
            "workflow_id": wf,
            "conflict": True,
        }

    try:
        if document.status == Document.STATUS_FAILED:
            controls.resume_document(document)
        else:
            controls.start_document(document)
    except controls.ControlError as exc:
        return {
            "ok": False,
            "content": exc.message,
            "workflow_id": None,
            "conflict": True,
        }
    except Exception:  # noqa: BLE001 - surface a retryable failure
        logger.exception("Temporal retry failed for document %s", document.pk)
        return {
            "ok": False,
            "content": "Still couldn't start processing — please try again shortly.",
            "workflow_id": None,
            "conflict": False,
        }
    return {
        "ok": True,
        "content": (
            f"Restarted processing for “{document.title}” on Temporal "
            f"(workflow id: {wf})."
        ),
        "workflow_id": wf,
        "conflict": False,
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
