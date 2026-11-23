from .interfaces import ProcessingBackend, ProcessingRequest


class CeleryProcessingBackend(ProcessingBackend):
    """Document processing as a Celery workflow (``celery_workflow``)."""

    def dispatch(self, request: ProcessingRequest) -> str:
        from uuid import uuid4

        from documents.models import Document

        from . import celery_workflow

        run_id = str(uuid4())
        Document.objects.filter(pk=request.document_id).update(
            processing_task_id=run_id, processing_backend="celery"
        )
        celery_workflow.start(request.document_id, run_id)
        return run_id

    def get_status(self, job_id: str) -> dict:
        from . import celery_workflow

        return celery_workflow.describe(job_id)

    def pause(self, job_id: str) -> None:
        """Nothing to send: the run parks when it sees the PAUSED row."""

    def resume(self, document_id: str) -> str:
        from . import celery_workflow

        return celery_workflow.resume(document_id)

    def cancel(self, job_id: str) -> None:
        from . import celery_workflow

        celery_workflow.revoke_run(job_id)
