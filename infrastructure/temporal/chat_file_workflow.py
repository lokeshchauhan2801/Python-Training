"""Temporal workflow for processing a chat file only inside its chat.

Same controls as ``DocumentProcessingWorkflow`` (``pause`` / ``resume``
signals, workflow cancel, a ``get_state`` query) but the stages write to the
chat session (``chat/services/chat_processing.py``), never to ``Document``,
``DocumentChunk`` or the vector store: the file's text is extracted, the LLM
summarises it, and only the summary is kept in the session. A pause takes
effect before the next stage. Django imports stay inside the activity bodies.
"""
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, is_cancelled_exception
from temporalio.workflow import ActivityCancellationType


async def _run_stage(name: str, params: dict) -> dict:
    from asgiref.sync import sync_to_async

    from chat.services import chat_processing

    func = getattr(chat_processing, f"stage_{name}")
    try:
        return await sync_to_async(func, thread_sensitive=False)(params)
    except chat_processing.PermanentFailure as exc:
        raise ApplicationError(str(exc), non_retryable=True) from exc


@activity.defn(name="chatfile_mark_processing")
async def chatfile_mark_processing_activity(params: dict) -> dict:
    return await _run_stage("mark_processing", params)


@activity.defn(name="chatfile_extract_text")
async def chatfile_extract_text_activity(params: dict) -> dict:
    return await _run_stage("extract_text", params)


@activity.defn(name="chatfile_summarize")
async def chatfile_summarize_activity(params: dict) -> dict:
    return await _run_stage("summarize", params)


@activity.defn(name="chatfile_mark_completed")
async def chatfile_mark_completed_activity(params: dict) -> dict:
    return await _run_stage("mark_completed", params)


@activity.defn(name="chatfile_discard_cancelled")
async def chatfile_discard_cancelled_activity(params: dict) -> dict:
    return await _run_stage("discard_cancelled", params)


CHAT_FILE_ACTIVITIES = [
    chatfile_mark_processing_activity,
    chatfile_extract_text_activity,
    chatfile_summarize_activity,
    chatfile_mark_completed_activity,
    chatfile_discard_cancelled_activity,
]

_STAGES = (
    ("mark_processing", chatfile_mark_processing_activity),
    ("extract_text", chatfile_extract_text_activity),
    ("summarize", chatfile_summarize_activity),
    ("mark_completed", chatfile_mark_completed_activity),
)


@workflow.defn(name="ChatFileProcessingWorkflow")
class ChatFileProcessingWorkflow:
    def __init__(self) -> None:
        self._paused = False
        self._current = ""
        self._completed: list = []

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.run
    async def run(self, params: dict) -> dict:
        result = {"status": "ready", "chunk_count": 0}
        try:
            for slug, activity_fn in _STAGES:
                await workflow.wait_condition(lambda: not self._paused)
                self._current = slug
                stage_result = await workflow.execute_activity(
                    activity_fn,
                    params,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                if isinstance(stage_result, dict):
                    if stage_result.get("stale"):
                        return {"status": "stale", "chunk_count": 0}
                    if "chunk_count" in stage_result:
                        result["chunk_count"] = stage_result["chunk_count"]
                self._completed.append(slug)
        except BaseException as exc:
            if not is_cancelled_exception(exc):
                raise
            await workflow.execute_activity(
                chatfile_discard_cancelled_activity,
                params,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
        self._current = ""
        return result

    @workflow.query
    def get_state(self) -> dict:
        return {
            "paused": self._paused,
            "stage": self._current,
            "completed": list(self._completed),
        }
