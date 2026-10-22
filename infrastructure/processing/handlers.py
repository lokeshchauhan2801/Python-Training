"""Shared surface-routing work function for document processing.

This module is the convergence point every processing backend builds on. It
knows how to run exactly one processing attempt for a given surface and provides
a bounded retry wrapper used by the Kafka handler. All Django/surface imports
are deferred inside the functions so importing this module never triggers Django
app loading or a broker connection.

In NEW only the ``documents`` surface is wired (OLD also had ``scrapper``). The
``scrapper`` surface is kept as a reachable-but-unimplemented branch so the
abstraction survives for a later phase.
"""

import logging

logger = logging.getLogger(__name__)

SURFACE_DOCS = "documents"
SURFACE_SCRAPPER = "scrapper"

MAX_ATTEMPTS = 3


def run_document_processing(surface, document_id, backend, company_id="", user_id=""):
    """Run exactly ONE processing attempt for the given surface.

    Returns a result dict. For the documents surface the result includes a
    ``chunk_count`` injected from the DB after processing (process_document_task
    itself returns only {status, document_id}). ``company_id``/``user_id`` are
    accepted-but-ignored by documents processing; they exist for signature
    parity with the event payload and the (future) scrapper surface.
    """
    if surface == SURFACE_SCRAPPER:


        raise ValueError("scrapper surface not wired in Phase 4")

    from documents.models import Document
    from documents.tasks import process_document_task

    result = process_document_task.run(
        document_id,
        company_id=company_id,
        user_id=user_id,
        backend=backend,
    )
    chunk_count = (
        Document.objects.filter(pk=document_id)
        .values_list("chunk_count", flat=True)
        .first()
    )
    result["chunk_count"] = chunk_count or 0
    return result


def run_with_retry(surface, document_id, backend, company_id="", user_id=""):
    """Run processing with a bounded number of attempts (MAX_ATTEMPTS).

    Used by the Kafka handler. The Temporal workflow owns its own retry cap, so
    it does not call this wrapper.
    """
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _record_attempt(surface, document_id, attempt)
        try:
            return run_document_processing(
                surface,
                document_id,
                backend,
                company_id=company_id,
                user_id=user_id,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Processing attempt %s/%s failed for %s/%s: %s",
                attempt,
                MAX_ATTEMPTS,
                surface,
                document_id,
                exc,
            )
    raise last_exc


def _record_attempt(surface, document_id, attempt):
    """Persist the current attempt number on the surface's Document row.

    A single disjoint-column UPDATE so it never collides with the processing
    writes. Wrapped so attempt bookkeeping never masks the real error.
    """
    try:
        if surface == SURFACE_SCRAPPER:
            raise ValueError("scrapper surface not wired in Phase 4")

        from documents.models import Document

        Document.objects.filter(pk=document_id).update(processing_attempts=attempt)
    except Exception as exc:
        logger.warning(
            "Could not record attempt %s for %s/%s: %s",
            attempt,
            surface,
            document_id,
            exc,
        )
