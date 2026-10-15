"""Temporal workflow + activity for document processing.

Temporal owns the retry cap here via ``RetryPolicy(maximum_attempts=3)`` (one
attempt plus two retries), so the activity runs processing exactly once and must
NOT call ``run_with_retry``. The module-level decorators are the only temporalio
imports that run at import time; every Django/ORM import stays inside the
activity body so importing this module never touches the database or connects.
"""
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@activity.defn(name="process_document_activity")
async def process_document_activity(params: dict) -> dict:
    document_id = params["document_id"]
    surface = params.get("surface", "documents")
    backend = params.get("backend", "temporal")
    company_id = params.get("company_id", "")
    user_id = params.get("user_id", "")

    from asgiref.sync import sync_to_async

    from infrastructure.processing.handlers import (
        _record_attempt,
        run_document_processing,
    )

    await sync_to_async(_record_attempt, thread_sensitive=False)(
        surface, document_id, activity.info().attempt
    )

    return await sync_to_async(run_document_processing, thread_sensitive=False)(
        surface=surface,
        document_id=document_id,
        backend=backend,
        company_id=company_id,
        user_id=user_id,
    )


@workflow.defn(name="DocumentProcessingWorkflow")
class DocumentProcessingWorkflow:
    @workflow.run
    async def run(self, params: dict) -> dict:
        return await workflow.execute_activity(
            process_document_activity,
            params,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
