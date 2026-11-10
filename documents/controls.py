"""Start / stop / resume / cancel / reprocess a library Document via Temporal.

Stop pauses: the document becomes PAUSED and its workflow gets a ``pause``
signal, so it stays open in Temporal and waits (see
``infrastructure/temporal/document_workflow.py``). From there:

* Resume sends ``resume`` and the same run continues from where it stopped.
  When no run is open (it was never dispatched, or an older Stop terminated
  it) a new run starts and skips the stages that already completed.
* Cancel cancels the workflow and discards the work done so far; the document
  is UPLOADED again and processing it starts from the beginning.

``start_document`` / ``reprocess_document`` keep the ``DocumentAdmin``
semantics (``dispatch_view``, ``_reset_for_reprocess``).
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from documents.models import Document, DocumentChunk, DocumentStage
from documents.services import (
    _mark_document_failed,
    discard_processing,
    reset_all_stages,
)
from documents.tasks import enqueue_document_processing
from infrastructure.processing.temporal_backend import TemporalProcessingBackend

# What a Stop recorded before Stop became a pause (and what
# DocumentAdmin.temporal_cancel_view still records): such rows are "stopped".
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


def _workflow_id(document: Document) -> str:
    return f"doc-{document.pk}"


def pause_document(document: Document) -> ControlResult:
    """Stop a queued or running document so it can be resumed or cancelled.

    The row becomes PAUSED first (stages check it between batches), then the
    workflow gets a ``pause`` signal. A queued row whose workflow has not
    started yet simply never starts (the dispatch skips non-QUEUED rows).
    """
    _check_source(document)
    paused = Document.objects.filter(
        pk=document.pk,
        status__in=(Document.STATUS_QUEUED, Document.STATUS_PROCESSING),
    ).update(status=Document.STATUS_PAUSED, updated_at=timezone.now())
    if not paused:
        raise ControlError("Only a queued or running document can be stopped.")
    if document.processing_backend == "temporal":
        TemporalProcessingBackend().signal(_workflow_id(document), "pause")
    return ControlResult(
        True,
        Document.STATUS_PAUSED,
        "Paused. Resume to continue from here, or cancel to discard it.",
    )


def _resume_or_restart(document_id: str) -> None:
    """After commit: wake the open run, or start a new one when none is open."""
    if TemporalProcessingBackend().signal(f"doc-{document_id}", "resume"):
        return
    enqueue_document_processing(document_id, backend="temporal")


def resume_document(document: Document) -> ControlResult:
    """Continue a PAUSED, stopped or failed document from where it stopped."""
    with transaction.atomic():
        document = _locked(document)
        _check_source(document)
        if document.status == Document.STATUS_PAUSED:
            Document.objects.filter(pk=document.pk).update(
                status=Document.STATUS_PROCESSING,
                processing_error="",
                updated_at=timezone.now(),
            )
            document_id = str(document.pk)
            # Signal only once PROCESSING is committed: a stage that read
            # PAUSED would pause the run again.
            transaction.on_commit(lambda: _resume_or_restart(document_id))
        elif document.status == Document.STATUS_FAILED:
            enqueue_document_processing(str(document.pk), backend="temporal")
        else:
            raise ControlError("Only a paused, stopped or failed document can be resumed.")
    return ControlResult(
        True,
        Document.STATUS_PROCESSING,
        "Resuming processing from where it stopped.",
    )


_CANCELLABLE = (
    Document.STATUS_QUEUED,
    Document.STATUS_PROCESSING,
    Document.STATUS_PAUSED,
    Document.STATUS_FAILED,
)


def cancel_document(document: Document) -> ControlResult:
    """Discard a paused, stopped, failed or running document's progress.

    The work is undone at once (the row is UPLOADED again, so Process starts
    from the beginning) and an open workflow is cancelled; it repeats the
    cleanup after any in-flight stage ends and closes as Cancelled.
    No row lock: the cancel request is network IO.
    """
    _check_source(document)
    document.refresh_from_db()
    if document.status not in _CANCELLABLE:
        raise ControlError("Only a stopped, failed or running document can be cancelled.")
    discard_processing(document)
    if document.processing_backend == "temporal":
        TemporalProcessingBackend().cancel(_workflow_id(document))
    return ControlResult(
        True,
        Document.STATUS_UPLOADED,
        "Cancelled. Its progress was discarded; process it again to start over.",
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


def is_paused(document: Document) -> bool:
    return document.status == Document.STATUS_PAUSED


def is_stopped(document: Document) -> bool:
    """A FAILED row from an older Stop (before Stop became a pause)."""
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
