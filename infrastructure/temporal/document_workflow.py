"""Temporal workflow + per-stage activities for document processing.

Processing is split into seven per-stage activities so a crash resumes at the
failed stage and each stage is a distinct Temporal Web UI event. Temporal owns
the retry cap via ``RetryPolicy(maximum_attempts=3)`` (one attempt plus two
retries) per activity.

Pause, resume and cancel (``documents.controls``):

* ``pause`` signal: the workflow waits before its next stage. A long stage
  (embedding) also stops at its next batch and returns ``{"paused": True}``;
  the workflow then waits and runs that stage again on resume, which continues
  from ``Document.processing_checkpoint``. The run stays open (Running) while
  paused, and the signals are events in its history.
* ``resume`` signal: the same run continues.
* Workflow cancel: after any in-flight stage ends, ``discard_processing``
  undoes the run's work and the run closes as Cancelled. The module-level decorators are the only temporalio
imports that run at import time; every Django/ORM import stays inside an
activity body so importing this module never touches the database or connects.
"""
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import is_cancelled_exception
from temporalio.workflow import ActivityCancellationType


@activity.defn(name="stage_mark_processing")
async def stage_mark_processing_activity(params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from documents.services import stage_mark_processing
    from infrastructure.processing.handlers import _record_attempt


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


@activity.defn(name="stage_discard_cancelled")
async def stage_discard_cancelled_activity(params: dict) -> dict:
    """Cancel cleanup: undo the run's work unless the document was re-queued."""
    from asgiref.sync import sync_to_async

    return await sync_to_async(_discard_cancelled, thread_sensitive=False)(
        params["document_id"]
    )


def _discard_cancelled(document_id: str) -> dict:
    from documents.models import Document
    from documents.services import discard_processing

    document = Document.objects.filter(pk=document_id).first()
    # The Cancel control already reset it to UPLOADED; anything else means the
    # user processed it again meanwhile, so leave the new run's data alone.
    if document is None or document.status != Document.STATUS_UPLOADED:
        return {"discarded": False}
    discard_processing(document)
    return {"discarded": True}


_STAGE_SEQUENCE = (
    ("mark_processing", stage_mark_processing_activity),
    ("extract_text", stage_extract_text_activity),
    ("chunk_text", stage_chunk_text_activity),
    ("delete_existing_chunks", stage_delete_existing_chunks_activity),
    ("embed_and_store_chunks", stage_embed_and_store_chunks_activity),
    ("mark_completed", stage_mark_completed_activity),
    ("notify_completed", stage_notify_completed_activity),
)


# No pause point before this stage: once ``mark_completed`` has run the
# document is READY, so a late pause must not hold the run open.
_NO_PAUSE_BEFORE = {"notify_completed"}


@workflow.defn(name="DocumentProcessingWorkflow")
class DocumentProcessingWorkflow:
    def __init__(self) -> None:
        self._stages: dict = {}
        self._paused = False
        self._current = ""

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.run
    async def run(self, params: dict) -> dict:
        result = {
            "status": "ready",
            "chunk_count": 0,
            "chunks_stored": 0,
            "error_message": "",
        }
        try:
            for slug, activity_fn in _STAGE_SEQUENCE:
                while True:
                    if slug not in _NO_PAUSE_BEFORE:
                        await workflow.wait_condition(lambda: not self._paused)
                    self._current = slug
                    stage_result = await workflow.execute_activity(
                        activity_fn,
                        params,
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                        # On cancel, wait for the running stage to end so the
                        # cleanup below runs after its last write.
                        cancellation_type=(
                            ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                        ),
                    )
                    if isinstance(stage_result, dict) and stage_result.get("paused"):
                        # The stage stopped at a checkpoint: wait, then run it
                        # again from that checkpoint.
                        self._paused = True
                        continue
                    break
                self._stages[slug] = stage_result
                if isinstance(stage_result, dict):
                    if "chunk_count" in stage_result:
                        result["chunk_count"] = stage_result["chunk_count"]
                    if "chunks_stored" in stage_result:
                        result["chunks_stored"] = stage_result["chunks_stored"]
        except BaseException as exc:
            if not is_cancelled_exception(exc):
                raise
            await workflow.execute_activity(
                stage_discard_cancelled_activity,
                params,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
        self._current = ""
        return result

    @workflow.query
    def get_stages(self) -> dict:
        return dict(self._stages)

    @workflow.query
    def get_state(self) -> dict:
        return {
            "paused": self._paused,
            "stage": self._current,
            "completed": list(self._stages),
        }
