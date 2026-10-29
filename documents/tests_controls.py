"""documents.controls and the task-id guard on the queued dispatch."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from chat.file_test_support import fake_enqueue
from company.models import Company
from documents import controls, tasks
from documents import services as document_services
from documents.models import Document, DocumentChunk, DocumentStage

User = get_user_model()

_ENQUEUE = "documents.controls.enqueue_document_processing"
_BACKEND = "documents.controls.TemporalProcessingBackend"


class _ControlsBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            email="controls-admin@example.com", password="pw"
        )
        self.company = Company.objects.create(
            owner=self.user, name="Controls Co", slug="controls-co"
        )

    def make_doc(
        self,
        *,
        status=Document.STATUS_PROCESSING,
        backend="temporal",
        source=Document.SOURCE_LIBRARY,
        stage="extract_text",
        error="",
        with_progress=True,
    ):
        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            source=source,
            status=status,
            processing_backend=backend,
            processing_stage=stage,
            processing_progress=25,
            processing_error=error,
            chunk_count=1,
            processing_attempts=2,
            title="report.pdf",
            file_type="pdf",
        )
        if with_progress:
            document_services._ensure_stages(document)
            DocumentStage.objects.filter(
                document=document, name=document_services.STAGE_MARK_PROCESSING
            ).update(status=DocumentStage.STATUS_COMPLETED)
            DocumentChunk.objects.create(
                company=self.company,
                document=document,
                chunk_index=0,
                page_number=1,
                content="kept",
            )
        return document

    def mark_stage_status(self, document):
        return DocumentStage.objects.get(
            document=document, name=document_services.STAGE_MARK_PROCESSING
        ).status

    def fields(self, document):
        document.refresh_from_db()
        return (
            document.status,
            document.processing_stage,
            document.processing_error,
            document.processing_progress,
            document.processing_backend,
        )


class StopDocumentTests(_ControlsBase):
    def test_temporal_doc_terminates_and_marks_stopped(self):
        document = self.make_doc()
        with mock.patch(_BACKEND) as Backend:
            result = controls.stop_document(document)
        Backend.return_value.terminate.assert_called_once_with(f"doc-{document.pk}")
        self.assertEqual(
            result,
            controls.ControlResult(
                True,
                Document.STATUS_FAILED,
                "Workflow terminated and document marked stopped.",
            ),
        )
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_FAILED)
        self.assertEqual(document.processing_stage, "failed")
        self.assertEqual(document.processing_error, controls.STOPPED_BY_USER_MESSAGE)
        self.assertTrue(controls.is_stopped(document))
        # Resume contract: completed stage and chunk survive.
        self.assertEqual(self.mark_stage_status(document), DocumentStage.STATUS_COMPLETED)
        self.assertEqual(DocumentChunk.objects.filter(document=document).count(), 1)

    def test_non_temporal_doc_is_marked_without_terminate(self):
        document = self.make_doc(status=Document.STATUS_QUEUED, backend="celery")
        with mock.patch(_BACKEND) as Backend:
            result = controls.stop_document(document)
        Backend.return_value.terminate.assert_not_called()
        self.assertEqual(
            result.message,
            "Marked stopped; the worker cannot be force-killed for this backend.",
        )
        document.refresh_from_db()
        self.assertTrue(controls.is_stopped(document))

    def test_guards(self):
        chat_doc = self.make_doc(source=Document.SOURCE_CHAT, with_progress=False)
        with mock.patch(_BACKEND) as Backend:
            with self.assertRaises(controls.ControlError) as ctx:
                controls.stop_document(chat_doc)
            self.assertEqual(ctx.exception.message, "Only library documents can be processed.")
            for status in (
                Document.STATUS_READY,
                Document.STATUS_FAILED,
                Document.STATUS_UPLOADED,
            ):
                document = self.make_doc(status=status, with_progress=False)
                with self.assertRaises(controls.ControlError) as ctx:
                    controls.stop_document(document)
                self.assertEqual(
                    ctx.exception.message,
                    "Only a queued or running document can be stopped.",
                )
        Backend.return_value.terminate.assert_not_called()


class ResumeDocumentTests(_ControlsBase):
    def test_failed_doc_resumes_without_reset(self):
        document = self.make_doc(
            status=Document.STATUS_FAILED,
            stage="failed",
            error=controls.STOPPED_BY_USER_MESSAGE,
        )
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            result = controls.resume_document(document)
        enqueue.assert_called_once_with(str(document.pk), backend="temporal")
        self.assertEqual(
            result,
            controls.ControlResult(
                True,
                Document.STATUS_QUEUED,
                "Resuming processing via Temporal from the failed stage.",
            ),
        )
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_QUEUED)
        self.assertFalse(controls.is_stopped(document))
        self.assertEqual(document.chunk_count, 1)
        self.assertEqual(self.mark_stage_status(document), DocumentStage.STATUS_COMPLETED)
        self.assertEqual(DocumentChunk.objects.filter(document=document).count(), 1)

    def test_guards(self):
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            chat_doc = self.make_doc(
                source=Document.SOURCE_CHAT,
                status=Document.STATUS_FAILED,
                with_progress=False,
            )
            with self.assertRaises(controls.ControlError) as ctx:
                controls.resume_document(chat_doc)
            self.assertEqual(ctx.exception.message, "Only library documents can be processed.")
            for status in (
                Document.STATUS_READY,
                Document.STATUS_QUEUED,
                Document.STATUS_PROCESSING,
                Document.STATUS_UPLOADED,
            ):
                document = self.make_doc(status=status, with_progress=False)
                with self.assertRaises(controls.ControlError) as ctx:
                    controls.resume_document(document)
                self.assertEqual(
                    ctx.exception.message, "Only a failed document can be retried."
                )
        enqueue.assert_not_called()

    def test_stale_failed_instance_is_rejected(self):
        """V7: the row is re-read under a lock, so a second resume cannot enqueue."""
        document = self.make_doc(status=Document.STATUS_FAILED, stage="failed")
        stale = Document.objects.get(pk=document.pk)
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            controls.resume_document(document)
            self.assertEqual(stale.status, Document.STATUS_FAILED)
            with self.assertRaises(controls.ControlError) as ctx:
                controls.resume_document(stale)
        self.assertEqual(ctx.exception.message, "Only a failed document can be retried.")
        enqueue.assert_called_once()


class StartDocumentTests(_ControlsBase):
    def test_uploaded_doc_is_queued(self):
        document = self.make_doc(
            status=Document.STATUS_UPLOADED,
            backend="celery",
            stage="",
            with_progress=False,
        )
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            result = controls.start_document(document)
        enqueue.assert_called_once_with(str(document.pk), backend="temporal")
        self.assertEqual(
            result,
            controls.ControlResult(
                True, Document.STATUS_QUEUED, "Queued for processing via Temporal."
            ),
        )
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_QUEUED)
        self.assertEqual(document.processing_backend, "temporal")
        self.assertEqual(document.chunk_count, 0)
        self.assertEqual(document.processing_attempts, 0)

    def test_guards(self):
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            chat_doc = self.make_doc(
                source=Document.SOURCE_CHAT,
                status=Document.STATUS_UPLOADED,
                with_progress=False,
            )
            with self.assertRaises(controls.ControlError) as ctx:
                controls.start_document(chat_doc)
            self.assertEqual(ctx.exception.message, "Only library documents can be processed.")
            for status in (
                Document.STATUS_QUEUED,
                Document.STATUS_PROCESSING,
                Document.STATUS_FAILED,
                Document.STATUS_READY,
            ):
                document = self.make_doc(status=status, with_progress=False)
                with self.assertRaises(controls.ControlError) as ctx:
                    controls.start_document(document)
                self.assertEqual(
                    ctx.exception.message, "This document has already been started."
                )
        enqueue.assert_not_called()

    def test_stale_uploaded_instance_is_rejected(self):
        document = self.make_doc(status=Document.STATUS_UPLOADED, with_progress=False)
        stale = Document.objects.get(pk=document.pk)
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            controls.start_document(document)
            with self.assertRaises(controls.ControlError):
                controls.start_document(stale)
        enqueue.assert_called_once()


class ReprocessDocumentTests(_ControlsBase):
    def test_ready_doc_is_fully_reset_and_queued(self):
        document = self.make_doc(status=Document.STATUS_READY, backend="celery", stage="ready")
        DocumentStage.objects.filter(document=document).update(
            status=DocumentStage.STATUS_COMPLETED
        )
        deleted_chunk = DocumentChunk.objects.create(
            company=self.company, document=document, chunk_index=1,
            page_number=1, content="old",
        )
        deleted_chunk.delete()  # soft delete; the reset hard-deletes it too
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            result = controls.reprocess_document(document)
        enqueue.assert_called_once_with(str(document.pk), backend="temporal")
        self.assertEqual(result.status, Document.STATUS_QUEUED)
        self.assertTrue(result.ok)
        self.assertEqual(DocumentChunk.all_objects.filter(document=document).count(), 0)
        self.assertFalse(
            DocumentStage.objects.filter(document=document)
            .exclude(status=DocumentStage.STATUS_PENDING)
            .exists()
        )
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_QUEUED)
        self.assertEqual(document.chunk_count, 0)
        self.assertEqual(document.processing_attempts, 0)
        self.assertEqual(document.processing_progress, 0)
        self.assertEqual(document.processing_error, "")
        self.assertEqual(document.processing_backend, "temporal")

    def test_chat_source_is_rejected(self):
        document = self.make_doc(
            source=Document.SOURCE_CHAT, status=Document.STATUS_READY
        )
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            with self.assertRaises(controls.ControlError) as ctx:
                controls.reprocess_document(document)
        self.assertEqual(ctx.exception.message, "Only library documents can be processed.")
        enqueue.assert_not_called()
        self.assertEqual(DocumentChunk.objects.filter(document=document).count(), 1)


class StateHelperTests(_ControlsBase):
    def test_is_stopped_only_for_exact_marker(self):
        cases = [
            (Document.STATUS_FAILED, "Processing stopped by user.", True),
            (Document.STATUS_FAILED, "  Processing stopped by user.  ", True),
            (Document.STATUS_FAILED, "Processing stopped by user. Then it broke", False),
            (Document.STATUS_FAILED, "Could not queue document for processing: x", False),
            (Document.STATUS_FAILED, "", False),
            (Document.STATUS_QUEUED, "Processing stopped by user.", False),
        ]
        for status, error, expected in cases:
            document = Document(status=status, processing_error=error)
            self.assertEqual(controls.is_stopped(document), expected, (status, error))

    def test_current_stage(self):
        document = self.make_doc(status=Document.STATUS_PROCESSING)
        # mark_processing completed, the rest pending: the first not completed.
        self.assertEqual(
            controls.current_stage(document),
            document_services.STAGE_EXTRACT_TEXT.replace("_", " "),
        )
        embed = document_services.DOCUMENT_STAGES[-1]
        DocumentStage.objects.filter(document=document, name=embed).update(
            status=DocumentStage.STATUS_FAILED
        )
        self.assertEqual(controls.current_stage(document), embed.replace("_", " "))

    def test_current_stage_without_stage_rows(self):
        """V6: "failed" and other status-like stages give ""."""
        for stage in ("failed", "", "uploaded", "queued", "ready"):
            document = self.make_doc(
                status=Document.STATUS_FAILED, stage=stage, with_progress=False
            )
            self.assertEqual(controls.current_stage(document), "", stage)
        document = self.make_doc(stage="embed_and_store_chunks", with_progress=False)
        self.assertEqual(controls.current_stage(document), "embed and store chunks")


class AdminParityTests(_ControlsBase):
    """stop/resume leave the row exactly as the admin cancel/retry views do."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def test_stop_matches_temporal_cancel_view(self):
        for backend in ("temporal", "celery"):
            via_admin = self.make_doc(backend=backend)
            via_controls = self.make_doc(backend=backend)
            with mock.patch("documents.admin.TemporalProcessingBackend"):
                resp = self.client.post(
                    reverse("admin:documents_document_temporal_cancel", args=[via_admin.pk])
                )
            self.assertEqual(resp.status_code, 200)
            with mock.patch(_BACKEND):
                result = controls.stop_document(via_controls)
            self.assertEqual(resp.json()["message"], result.message)
            self.assertEqual(resp.json()["status"], result.status)
            via_admin.refresh_from_db()
            self.assertEqual(via_admin.processing_error, controls.STOPPED_BY_USER_MESSAGE)
            self.assertTrue(controls.is_stopped(via_admin))
            self.assertEqual(self.fields(via_admin), self.fields(via_controls))

    def test_resume_matches_temporal_retry_view(self):
        via_admin = self.make_doc(
            status=Document.STATUS_FAILED, stage="failed",
            error=controls.STOPPED_BY_USER_MESSAGE,
        )
        via_controls = self.make_doc(
            status=Document.STATUS_FAILED, stage="failed",
            error=controls.STOPPED_BY_USER_MESSAGE,
        )
        with mock.patch(
            "documents.admin.enqueue_document_processing", side_effect=fake_enqueue
        ) as admin_enqueue:
            resp = self.client.post(
                reverse("admin:documents_document_temporal_retry", args=[via_admin.pk])
            )
        self.assertEqual(resp.status_code, 202)
        with mock.patch(_ENQUEUE, side_effect=fake_enqueue) as enqueue:
            result = controls.resume_document(via_controls)
        admin_enqueue.assert_called_once_with(str(via_admin.pk), backend="temporal")
        enqueue.assert_called_once_with(str(via_controls.pk), backend="temporal")
        self.assertEqual(resp.json()["message"], result.message)
        self.assertEqual(resp.json()["status"], result.status)
        self.assertEqual(self.fields(via_admin), self.fields(via_controls))
        self.assertEqual(
            self.mark_stage_status(via_admin), self.mark_stage_status(via_controls)
        )


class QueuedDispatchGuardTests(_ControlsBase):
    """Review-3 F4 (C12b) and the executor half of C10."""

    def enqueue_capturing(self, document):
        with self.captureOnCommitCallbacks(execute=True):
            task_id, _backend = tasks.enqueue_document_processing(
                str(document.pk), backend="temporal"
            )
        return task_id

    def run_captured(self, call, dispatch):
        fn, *args = call.args
        with mock.patch("documents.tasks.close_old_connections"), mock.patch(
            "documents.tasks.dispatch_document_processing", dispatch
        ):
            fn(*args)

    def test_stop_between_enqueue_and_dispatch_wins(self):
        document = self.make_doc(status=Document.STATUS_FAILED, stage="failed")
        with mock.patch.object(tasks._dispatch_executor, "submit") as submit:
            task_id = self.enqueue_capturing(document)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[1:], (str(document.pk), "temporal", task_id))
        document.refresh_from_db()
        with mock.patch(_BACKEND):
            controls.stop_document(document)
        dispatch = mock.Mock()
        self.run_captured(submit.call_args, dispatch)
        dispatch.assert_not_called()
        document.refresh_from_db()
        self.assertTrue(controls.is_stopped(document))

    def test_double_enqueue_first_callable_skips(self):
        document = self.make_doc(status=Document.STATUS_FAILED, stage="failed")
        with mock.patch.object(tasks._dispatch_executor, "submit") as submit:
            self.enqueue_capturing(document)
            self.enqueue_capturing(document)
        self.assertEqual(submit.call_count, 2)
        first, second = submit.call_args_list
        dispatch = mock.Mock(return_value=("job", "temporal"))
        self.run_captured(first, dispatch)
        dispatch.assert_not_called()
        self.run_captured(second, dispatch)
        dispatch.assert_called_once_with(str(document.pk), backend="temporal")

    def test_two_argument_call_still_dispatches(self):
        document = self.make_doc(status=Document.STATUS_QUEUED)
        dispatch = mock.Mock(return_value=("job", "temporal"))
        with mock.patch("documents.tasks.close_old_connections"), mock.patch(
            "documents.tasks.dispatch_document_processing", dispatch
        ):
            tasks._dispatch_queued_document(str(document.pk), "temporal")
        dispatch.assert_called_once_with(str(document.pk), backend="temporal")

    def test_dispatch_failure_marks_queued_row_failed(self):
        document = self.make_doc(status=Document.STATUS_QUEUED, stage="queued")
        with mock.patch("documents.tasks.close_old_connections"), mock.patch(
            "documents.tasks.dispatch_document_processing",
            side_effect=RuntimeError("temporal unreachable"),
        ):
            tasks._dispatch_queued_document(str(document.pk), "temporal")
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_FAILED)
        self.assertEqual(document.processing_stage, "failed")
        self.assertEqual(
            document.processing_error,
            "Could not queue document for processing: temporal unreachable",
        )
        self.assertFalse(controls.is_stopped(document))
        self.assertEqual(controls.current_stage(document), "extract text")

    def test_dispatch_failure_leaves_a_non_queued_row_alone(self):
        document = self.make_doc(
            status=Document.STATUS_FAILED, stage="failed",
            error=controls.STOPPED_BY_USER_MESSAGE,
        )
        before = self.fields(document)
        with mock.patch("documents.tasks.close_old_connections"), mock.patch(
            "documents.tasks.dispatch_document_processing",
            side_effect=RuntimeError("temporal unreachable"),
        ):
            tasks._dispatch_queued_document(str(document.pk), "temporal")
        self.assertEqual(self.fields(document), before)
