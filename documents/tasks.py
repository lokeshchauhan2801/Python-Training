"""Celery task + backend-dispatch seam for document processing.

Mirrors the OLD ``docs/tasks.py`` but adapted to NEW's single-argument
``documents.services.process_document(document)``:

* ``process_document_task`` is the Celery ``shared_task`` every backend converges
  on (Celery ``.delay``/``apply_async``, the Kafka consumer via
  ``run_document_processing``, and the Temporal activity all call it). It loads
  the row, runs the URL download first when the source is a URL, then runs the
  synchronous ``process_document`` which owns the processing→ready/failed
  transition and the embed/no-key/cleanup contract. ``company_id``/``user_id``/
  ``backend`` are accepted for signature parity and ignored by the service.
* ``dispatch_document_processing`` is the routing seam the admin calls. It flips
  the row to QUEUED first (so the UI/notification poll observes the transition)
  then routes: DEBUG celery → inline ThreadPool; real celery → ``apply_async``;
  kafka/temporal → ``ProcessingDispatcher``.

Every celery/kafka/temporal import that could touch a broker stays lazy so this
module imports with no infra running.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from celery import shared_task
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from .models import Document
from .services import process_document

logger = logging.getLogger(__name__)

_local_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="document-index",
)


@shared_task(name="apps.documents.process_document")
def process_document_task(
    document_id: str,
    company_id: str | None = None,
    user_id: str | None = None,
    backend: str = "celery",
):
    """Download (if URL-sourced), extract, embed, and index one document.

    ``company_id``/``user_id``/``backend`` exist for signature parity with the
    event payload and the Kafka/Temporal surface router; the synchronous
    ``process_document`` service owns all status transitions and the embed/
    no-key/cleanup contract.
    """
    try:
        document = Document.objects.get(pk=document_id)
    except ObjectDoesNotExist:
        logger.error("Cannot process missing document %s", document_id)
        raise

    # URL-sourced documents have no stored file yet: download it first (SSRF
    # safe) and persist the file before extraction. A download failure marks the
    # document failed with a user-safe message and re-raises.
    if document.source_url and not document.file:
        from .url_source import UrlSourceError, download_source_to_document

        try:
            download_source_to_document(document)
        except UrlSourceError as exc:
            _mark_download_failed(document, str(exc))
            raise
        document.save(
            update_fields=[
                "file",
                "title",
                "file_type",
                "content_type",
                "file_size",
                "updated_at",
            ]
        )

    try:
        process_document(document)
    except Exception:
        logger.exception("Document processing failed for %s", document_id)
        raise
    return {"status": Document.STATUS_READY, "document_id": document_id}


def _mark_download_failed(document: Document, message: str) -> None:
    """Flip a URL-sourced document to failed when the download itself fails."""
    document.status = Document.STATUS_FAILED
    document.processing_stage = "failed"
    document.processing_progress = 0
    document.processing_error = message
    document.save(
        update_fields=[
            "status",
            "processing_stage",
            "processing_progress",
            "processing_error",
            "updated_at",
        ]
    )


def dispatch_document_processing(
    document_id: str, backend: str | None = None
) -> tuple[str, str]:
    """Dispatch processing for a document via the selected backend.

    ``backend`` selects the strategy (``celery``/``kafka``/``temporal``); when
    omitted the configured ``DOCUMENT_PROCESSING_BACKEND`` is used. The document
    is first flipped to QUEUED (stage/progress reset, ``processing_backend``
    recorded, ``processing_task_id`` set) so the changelist and the completion
    notification poll observe the transition.

    Routing:
      * ``celery``/``local-background`` under DEBUG → run inline on a managed
        background ThreadPool (no Redis/worker needed).
      * ``celery`` (non-DEBUG) → ``apply_async`` to a real Celery worker.
      * ``kafka``/``temporal``/other → ``ProcessingDispatcher`` strategy.
    """
    requested = (
        backend or getattr(settings, "DOCUMENT_PROCESSING_BACKEND", "celery")
    ).lower()

    document = Document.objects.filter(pk=document_id).first()
    task_id = str(uuid4())

    # Flip to QUEUED up front so the UI reflects the transition immediately and
    # the status poll can observe queued → processing → ready/failed.
    if document is not None:
        document.status = Document.STATUS_QUEUED
        document.processing_stage = "queued"
        document.processing_progress = 0
        document.processing_backend = requested
        document.processing_task_id = task_id
        document.processing_error = ""
        document.save(
            update_fields=[
                "status",
                "processing_stage",
                "processing_progress",
                "processing_backend",
                "processing_task_id",
                "processing_error",
                "updated_at",
            ]
        )

    if requested in {"celery", "local-background"} and settings.DEBUG:
        _local_executor.submit(
            process_document_task.run,
            document_id,
            backend="local-background",
        )
        return task_id, "local-background"

    if requested == "celery":
        task = process_document_task.apply_async(args=[document_id], retry=False)
        return task.id, "celery"

    # Kafka / Temporal (and any non-default backend) go through the strategy
    # dispatcher so the selected infrastructure handles the document. Lazy
    # import keeps this module broker-free at load time.
    from infrastructure.processing.dispatcher import ProcessingDispatcher
    from infrastructure.processing.interfaces import ProcessingRequest

    dispatcher = ProcessingDispatcher.from_name(requested)
    job_id = dispatcher.dispatch(
        ProcessingRequest(
            document_id=str(document_id),
            company_id=str(document.company_id)
            if document and document.company_id
            else "",
            user_id=str(document.uploaded_by_id) if document else "",
            backend=requested,
        )
    )
    return job_id, requested
