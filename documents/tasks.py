from __future__ import annotations
import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist
from django.db import close_old_connections
from django.db import transaction

from .models import Document
from .services import process_document

logger = logging.getLogger(__name__)

_local_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="document-index",
)
_dispatch_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="document-dispatch",
)


@shared_task(name="apps.documents.process_document")
def process_document_task(
    document_id: str,
    company_id: str | None = None,
    user_id: str | None = None,
    backend: str = "celery",
):

    try:
        document = Document.objects.get(pk=document_id)
    except ObjectDoesNotExist:
        logger.error("Cannot process missing document %s", document_id)
        raise


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


BACKENDS = ("celery", "kafka", "temporal", "local-background")


def _requested_backend(backend: str | None) -> str:
    """The backend the user picked. There is no default: every run names one."""
    requested = (backend or "").strip().lower()
    if requested not in BACKENDS:
        raise ValueError(
            f"Choose a processing backend ({', '.join(BACKENDS[:3])}); got {backend!r}."
        )
    return requested


def dispatch_document_processing(
    document_id: str, backend: str
) -> tuple[str, str]:
    requested = _requested_backend(backend)

    document = Document.objects.filter(pk=document_id).first()
    task_id = str(uuid4())


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

    if requested == "local-background":
        # Explicit in-process run on a background thread (no broker).
        transaction.on_commit(
            lambda: _local_executor.submit(
                process_document_task.run,
                document_id,
                backend="local-background",
            )
        )
        return task_id, "local-background"

    if requested == "celery":
        # The Celery workflow: one task per stage, chained through Redis and
        # run by ``celery -A base.celery worker`` (``manage.py celery_worker``).
        # The run starts once the QUEUED row above is committed.
        from infrastructure.processing import celery_workflow

        transaction.on_commit(
            lambda: celery_workflow.start(str(document_id), task_id)
        )
        return task_id, "celery"


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


def enqueue_document_processing(
    document_id: str, backend: str
) -> tuple[str, str]:

    requested = _requested_backend(backend)
    task_id = str(uuid4())

    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document_id)
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
        transaction.on_commit(
            lambda: _dispatch_executor.submit(
                _dispatch_queued_document, str(document_id), backend, task_id
            )
        )

    return task_id, requested


def _dispatch_queued_document(
    document_id: str, backend: str | None, task_id: str | None = None
) -> None:
    """Dispatch a previously queued document and persist transport failures.

    With ``task_id`` the dispatch only runs while the row is still QUEUED under
    that task id, so a Stop or a re-enqueue made after the enqueue wins.
    """
    close_old_connections()
    try:
        if task_id and not Document.objects.filter(
            pk=document_id,
            status=Document.STATUS_QUEUED,
            processing_task_id=task_id,
        ).exists():
            logger.info(
                "Skip dispatch of %s: stopped or re-queued since enqueue",
                document_id,
            )
            return
        dispatch_document_processing(document_id, backend=backend)
    except Exception as exc:
        logger.exception("Could not dispatch document %s", document_id)
        Document.objects.filter(
            pk=document_id, status=Document.STATUS_QUEUED
        ).update(
            status=Document.STATUS_FAILED,
            processing_stage="failed",
            processing_progress=0,
            processing_error=f"Could not queue document for processing: {exc}",
        )
    finally:
        close_old_connections()
