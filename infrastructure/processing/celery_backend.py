from .interfaces import ProcessingBackend, ProcessingRequest


class CeleryProcessingBackend(ProcessingBackend):
    def dispatch(self, request: ProcessingRequest) -> str:
        # Import here to avoid a broker/import touch at module load time.
        from documents.tasks import process_document_task

        result = process_document_task.delay(
            document_id=request.document_id,
            company_id=request.company_id,
            user_id=request.user_id,
        )
        return result.id

    def get_status(self, job_id: str) -> str:
        from celery.result import AsyncResult

        return AsyncResult(job_id).status
