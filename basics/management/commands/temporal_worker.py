"""Run the Temporal document-processing worker.

ALL temporalio imports live inside ``_run`` so ``--help`` never connects to a
Temporal server.
"""
import asyncio

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Temporal worker for document processing."

    def handle(self, *args, **options):
        asyncio.run(self._run())

    async def _run(self):
        from django.conf import settings
        from temporalio.client import Client
        from temporalio.worker import Worker

        from infrastructure.temporal.document_workflow import (
            DocumentProcessingWorkflow,
            stage_chunk_text_activity,
            stage_delete_existing_chunks_activity,
            stage_embed_and_store_chunks_activity,
            stage_extract_text_activity,
            stage_mark_completed_activity,
            stage_mark_processing_activity,
            stage_notify_completed_activity,
        )

        client = await Client.connect(settings.TEMPORAL_HOST)
        worker = Worker(
            client,
            task_queue="document-processing",
            workflows=[DocumentProcessingWorkflow],
            activities=[
                stage_mark_processing_activity,
                stage_extract_text_activity,
                stage_chunk_text_activity,
                stage_delete_existing_chunks_activity,
                stage_embed_and_store_chunks_activity,
                stage_mark_completed_activity,
                stage_notify_completed_activity,
            ],
        )
        self.stdout.write(
            "Starting Temporal worker on task queue 'document-processing'"
        )
        await worker.run()
