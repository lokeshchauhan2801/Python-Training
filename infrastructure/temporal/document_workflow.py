"""Temporal workflow + per-stage activities for document processing.

Processing is split into seven per-stage activities so a crash resumes at the
failed stage and each stage is a distinct Temporal Web UI event. Temporal owns
the retry cap via ``RetryPolicy(maximum_attempts=3)`` (one attempt plus two
retries) per activity. The module-level decorators are the only temporalio
imports that run at import time; every Django/ORM import stays inside an
activity body so importing this module never touches the database or connects.
"""
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="stage_mark_processing")
async def stage_mark_processing_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_mark_processing
    from infrastructure.processing.handlers import _record_attempt

    # The activity wrapper owns attempt bookkeeping (recorded once per run with
    # Temporal's own attempt counter); the stage function deliberately does not.
    await sync_to_async(_record_attempt, thread_sensitive=False)(
        params.get("surface", "documents"),
        params["document_id"],
        activity.info().attempt,
    )
    return await sync_to_async(stage_mark_processing, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_extract_text")
async def stage_extract_text_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_extract_text

    return await sync_to_async(stage_extract_text, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_chunk_text")
async def stage_chunk_text_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_chunk_text

    return await sync_to_async(stage_chunk_text, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_delete_existing_chunks")
async def stage_delete_existing_chunks_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_delete_existing_chunks

    return await sync_to_async(stage_delete_existing_chunks, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_embed_and_store_chunks")
async def stage_embed_and_store_chunks_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_embed_and_store_chunks

    return await sync_to_async(stage_embed_and_store_chunks, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_mark_completed")
async def stage_mark_completed_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_mark_completed

    return await sync_to_async(stage_mark_completed, thread_sensitive=False)(
        params["document_id"]
    )


@activity.defn(name="stage_notify_completed")
async def stage_notify_completed_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_notify_completed

    return await sync_to_async(stage_notify_completed, thread_sensitive=False)(
        params["document_id"]
    )


# Static (slug, activity) sequence — the single source of execution order. The
# workflow body stays deterministic by iterating this constant list only.
_STAGE_SEQUENCE = (
    ("mark_processing", stage_mark_processing_activity),
    ("extract_text", stage_extract_text_activity),
    ("chunk_text", stage_chunk_text_activity),
    ("delete_existing_chunks", stage_delete_existing_chunks_activity),
    ("embed_and_store_chunks", stage_embed_and_store_chunks_activity),
    ("mark_completed", stage_mark_completed_activity),
    ("notify_completed", stage_notify_completed_activity),
)


@workflow.defn(name="DocumentProcessingWorkflow")
class DocumentProcessingWorkflow:
    def __init__(self) -> None:
        # Determinism: __init__ sets per-run state only, nothing side-effecting.
        self._stages: dict = {}

    @workflow.run
    async def run(self, params: dict) -> dict:
        result = {
            "status": "ready",
            "chunk_count": 0,
            "chunks_stored": 0,
            "error_message": "",
        }
        for slug, activity_fn in _STAGE_SEQUENCE:
            stage_result = await workflow.execute_activity(
                activity_fn,
                params,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self._stages[slug] = stage_result
            if isinstance(stage_result, dict):
                if "chunk_count" in stage_result:
                    result["chunk_count"] = stage_result["chunk_count"]
                if "chunks_stored" in stage_result:
                    result["chunks_stored"] = stage_result["chunks_stored"]
        return result

    @workflow.query
    def get_stages(self) -> dict:
        return dict(self._stages)
