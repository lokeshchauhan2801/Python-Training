from .interfaces import ProcessingBackend, ProcessingRequest


class TemporalProcessingBackend(ProcessingBackend):
    TASK_QUEUE = "document-processing"

    def dispatch(self, request: ProcessingRequest) -> str:
        # The Temporal client is async; in a sync context we drive it via
        # asyncio.run so the admin/dispatch seam stays synchronous.
        import asyncio

        return asyncio.run(self._dispatch_async(request))

    async def _dispatch_async(self, request: ProcessingRequest) -> str:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        surface = (request.extra or {}).get("surface", "documents")
        handle = await client.start_workflow(
            "DocumentProcessingWorkflow",
            {
                "document_id": request.document_id,
                "company_id": request.company_id,
                "user_id": request.user_id,
                "surface": surface,
                "backend": request.backend,
            },
            id=f"doc-{request.document_id}",
            task_queue=self.TASK_QUEUE,
        )
        return handle.id

    def get_status(self, job_id: str) -> str:
        return "RUNNING"
