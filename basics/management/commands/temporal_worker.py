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
            process_document_activity,
        )

        client = await Client.connect(settings.TEMPORAL_HOST)
        worker = Worker(
            client,
            task_queue="document-processing",
            workflows=[DocumentProcessingWorkflow],
            activities=[process_document_activity],
        )
        self.stdout.write(
            "Starting Temporal worker on task queue 'document-processing'"
        )
        await worker.run()
