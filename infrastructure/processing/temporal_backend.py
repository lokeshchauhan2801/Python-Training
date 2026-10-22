from .interfaces import ProcessingBackend, ProcessingRequest


class TemporalProcessingBackend(ProcessingBackend):
    TASK_QUEUE = "document-processing"

    def dispatch(self, request: ProcessingRequest) -> str:


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

    def get_status(self, job_id: str) -> dict:


        import asyncio

        try:
            return asyncio.run(self._status_async(job_id))
        except Exception:
            return {
                "status": "unknown",
                "degraded": True,
                "run_id": None,
                "started": None,
                "closed": None,
                "result": None,
            }

    async def _status_async(self, job_id: str) -> dict:
        import temporalio.client as tc
        from django.conf import settings

        client = await tc.Client.connect(
            getattr(settings, "TEMPORAL_HOST", "localhost:7233")
        )
        handle = client.get_workflow_handle(job_id)


        desc = await handle.describe()
        return {
            "status": desc.status.name if desc.status else "unknown",
            "degraded": False,
            "run_id": desc.run_id,
            "started": desc.start_time.isoformat() if desc.start_time else None,
            "closed": desc.close_time.isoformat() if desc.close_time else None,
            "result": None,
        }
