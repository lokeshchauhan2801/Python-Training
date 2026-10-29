"""Start / stop / resume / reprocess a library Document through Temporal.

The semantics are copied from ``DocumentAdmin`` (``temporal_cancel_view``,
``temporal_retry_view``, ``dispatch_view`` and ``_reset_for_reprocess``) so the
chat page and the Documents menu move a row through the same states. The admin
views are not changed; they could later delegate here.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from documents.models import Document, DocumentChunk, DocumentStage
from documents.services import _mark_document_failed, reset_all_stages
from documents.tasks import enqueue_document_processing
from infrastructure.processing.temporal_backend import TemporalProcessingBackend

# Exactly what DocumentAdmin.temporal_cancel_view records.
STOPPED_BY_USER_MESSAGE = "Processing stopped by user."

_LIBRARY_ONLY = "Only library documents can be processed."
_STATUS_LIKE_STAGES = {"", "uploaded", "queued", "failed", "ready"}


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    status: str
    message: str


class ControlError(Exception):
    """A control is not allowed in the document's current state."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _check_source(document: Document) -> None:
    if document.source != Document.SOURCE_LIBRARY:
        raise ControlError(_LIBRARY_ONLY)


def _locked(document: Document) -> Document:
    return Document.objects.select_for_update().get(pk=document.pk)


def _reset_and_enqueue(document: Document):
    """``DocumentAdmin._reset_for_reprocess`` + the temporal dispatch."""
    DocumentChunk.all_objects.filter(document=document).hard_delete()
    reset_all_stages(document)
    document.chunk_count = 0
    document.processing_attempts = 0
    document.processing_progress = 0
    document.processing_error = ""
    document.processing_backend = "temporal"
    document.save(
        update_fields=[
            "chunk_count",
            "processing_attempts",
            "processing_progress",
            "processing_error",
            "processing_backend",
            "updated_at",
        ]
    )
    return enqueue_document_processing(str(document.pk), backend="temporal")


def start_document(document: Document) -> ControlResult:
    """First dispatch of an UPLOADED row."""
    with transaction.atomic():
        document = _locked(document)
        _check_source(document)
        if document.status != Document.STATUS_UPLOADED:
            raise ControlError("This document has already been started.")
        _reset_and_enqueue(document)
    return ControlResult(
        True, Document.STATUS_QUEUED, "Queued for processing via Temporal."
    )


def stop_document(document: Document) -> ControlResult:
    """``DocumentAdmin.temporal_cancel_view``. No lock: terminate is network IO."""
    _check_source(document)
    if document.status not in (
        Document.STATUS_QUEUED,
        Document.STATUS_PROCESSING,
    ):
        raise ControlError("Only a queued or running document can be stopped.")

    if document.processing_backend == "temporal":
        TemporalProcessingBackend().terminate(f"doc-{document.pk}")
        message = "Workflow terminated and document marked stopped."
    else:
        message = (
            "Marked stopped; the worker cannot be force-killed for this "
            "backend."
        )
    # Preserves chunk rows and completed stages (resume contract).
    _mark_document_failed(document, STOPPED_BY_USER_MESSAGE)
    return ControlResult(True, Document.STATUS_FAILED, message)


def resume_document(document: Document) -> ControlResult:
    """``DocumentAdmin.temporal_retry_view``: resume from the failed stage."""
    with transaction.atomic():
        document = _locked(document)
        _check_source(document)
        if document.status != Document.STATUS_FAILED:
            raise ControlError("Only a failed document can be retried.")
        enqueue_document_processing(str(document.pk), backend="temporal")
    return ControlResult(
        True,
        Document.STATUS_QUEUED,
        "Resuming processing via Temporal from the failed stage.",
    )


def reprocess_document(document: Document) -> ControlResult:
    """``DocumentAdmin.dispatch_view`` with backend="temporal": full reprocess."""
    with transaction.atomic():
        document = _locked(document)
        _check_source(document)
        _reset_and_enqueue(document)
    return ControlResult(
        True, Document.STATUS_QUEUED, "Task queued for processing via temporal."
    )


def is_stopped(document: Document) -> bool:
    return (
        document.status == Document.STATUS_FAILED
        and (document.processing_error or "").strip() == STOPPED_BY_USER_MESSAGE
    )


def _humanise(stage: str) -> str:
    return stage.replace("_", " ")


def current_stage(document: Document) -> str:
    """The stage a document is at, failed at or stopped at; "" when unknown."""
    stages = list(
        DocumentStage.objects.filter(document_id=document.pk)
        .order_by("order")
        .values_list("name", "status")
    )
    for name, status in stages:
        if status == DocumentStage.STATUS_FAILED:
            return _humanise(name)
    for name, status in stages:
        if status != DocumentStage.STATUS_COMPLETED:
            return _humanise(name)
    stage = (document.processing_stage or "").strip()
    if stage.lower() in _STATUS_LIKE_STAGES:
        return ""
    return _humanise(stage)
