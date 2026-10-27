

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from company.models import Company
from documents import services as document_services
from documents.models import Document, DocumentChunk, DocumentStage

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix="documents-controls-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class StageFailureStatusTests(TestCase):
    """ITEM 1: a stage failure must flip Document.status to FAILED (resume-safe)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(email="o1@example.com", password="x")
        self.company = Company.objects.create(
            owner=self.owner, name="SF", slug="sf"
        )
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_PROCESSING,
            processing_backend="temporal",
            processing_stage="extract_text",
            processing_progress=25,
            title="too-big.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("too-big.pdf", b"%PDF-1.4 fake"),
        )
        # A previously-completed stage + a persisted chunk that must survive.
        document_services._ensure_stages(self.document)
        DocumentStage.objects.filter(
            document=self.document,
            name=document_services.STAGE_MARK_PROCESSING,
        ).update(status=DocumentStage.STATUS_COMPLETED)
        self.chunk = DocumentChunk.objects.create(
            company=self.company,
            document=self.document,
            chunk_index=0,
            page_number=1,
            source_location="page 1",
            content="kept chunk",
        )

    def test_validation_failure_flips_document_to_failed_but_keeps_resume_state(self):
        err = document_services.DocumentValidationError(
            "This file is too large to index. Limit it to 10000 text chunks."
        )
        with mock.patch(
            "documents.services.extract_document_chunks", side_effect=err
        ):
            with self.assertRaises(document_services.DocumentValidationError):
                document_services.stage_extract_text(str(self.document.pk))

        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.STATUS_FAILED)
        self.assertIn("too large", self.document.processing_error)

        # The failed stage row is marked failed.
        extract_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_EXTRACT_TEXT
        )
        self.assertEqual(extract_stage.status, DocumentStage.STATUS_FAILED)

        # Resume-safe: the previously-completed stage + chunk are untouched.
        mark_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_MARK_PROCESSING
        )
        self.assertEqual(mark_stage.status, DocumentStage.STATUS_COMPLETED)
        self.assertTrue(
            DocumentChunk.objects.filter(pk=self.chunk.pk).exists()
        )


@override_settings(MEDIA_ROOT=_MEDIA)
class RetryEndpointTests(TestCase):
    """ITEM 2: the temporal_retry admin endpoint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@example.com", password="pw"
        )
        self.other = User.objects.create_user(
            email="other@example.com", password="x"
        )
        self.company = Company.objects.create(
            owner=self.admin, name="RC", slug="rc"
        )
        self.client = Client()
        self.client.force_login(self.admin)
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_FAILED,
            processing_backend="temporal",
            title="failed.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("failed.pdf", b"%PDF-1.4 fake"),
        )
        document_services._ensure_stages(self.document)
        DocumentStage.objects.filter(
            document=self.document,
            name=document_services.STAGE_MARK_PROCESSING,
        ).update(status=DocumentStage.STATUS_COMPLETED)
        self.chunk = DocumentChunk.objects.create(
            company=self.company,
            document=self.document,
            chunk_index=0,
            page_number=1,
            content="kept",
        )

    def _url(self, pk=None):
        return reverse(
            "admin:documents_document_temporal_retry", args=[pk or self.document.pk]
        )

    def test_post_resumes_via_temporal_without_resetting_completed_state(self):
        with mock.patch(
            "documents.admin.enqueue_document_processing",
            return_value=("tid", "temporal"),
        ) as enqueue:
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 202)
        data = resp.json()
        self.assertEqual(data["status"], Document.STATUS_QUEUED)
        self.assertEqual(data["backend"], "temporal")
        enqueue.assert_called_once_with(str(self.document.pk), backend="temporal")

        # HTTP-boundary check: the endpoint itself never resets chunks/stages
        # (it does not call _reset_for_reprocess). The actual resume behaviour
        # of the re-dispatch is proven in
        # test_resume_dispatch_preserves_completed_stages_end_to_end below.
        mark_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_MARK_PROCESSING
        )
        self.assertEqual(mark_stage.status, DocumentStage.STATUS_COMPLETED)
        self.assertTrue(DocumentChunk.objects.filter(pk=self.chunk.pk).exists())

    def test_resume_dispatch_preserves_completed_stages_end_to_end(self):
        """Drive the REAL re-dispatch stage so the resume contract is proven.

        The retry endpoint re-dispatches through ``stage_mark_processing`` (the
        first activity of a new Temporal run). Run it directly here — not a mock
        — against a document whose ``mark_processing`` stage already COMPLETED.
        It must leave that completed stage COMPLETED (so ``_stage_is_completed``
        short-circuits it on resume) while re-arming the not-completed stages to
        PENDING. This is what makes retry resume from the failed stage instead
        of restarting the whole pipeline.
        """
        # Also leave extract_text FAILED (as a real failed run would) to prove
        # it is re-armed to PENDING for re-execution.
        DocumentStage.objects.filter(
            document=self.document,
            name=document_services.STAGE_EXTRACT_TEXT,
        ).update(status=DocumentStage.STATUS_FAILED, detail="boom")

        document_services.stage_mark_processing(str(self.document.pk))

        mark_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_MARK_PROCESSING
        )
        # mark_processing re-runs itself every dispatch, so it ends COMPLETED.
        self.assertEqual(mark_stage.status, DocumentStage.STATUS_COMPLETED)

        extract_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_EXTRACT_TEXT
        )
        # The previously-failed stage is re-armed so it runs again on resume.
        self.assertEqual(extract_stage.status, DocumentStage.STATUS_PENDING)
        self.assertEqual(extract_stage.detail, "")

        # A downstream stage that had ALREADY completed must stay completed so
        # the resume short-circuits it rather than redoing the work.
        DocumentStage.objects.filter(
            document=self.document,
            name=document_services.STAGE_CHUNK_TEXT,
        ).update(status=DocumentStage.STATUS_COMPLETED)
        document_services.stage_mark_processing(str(self.document.pk))
        chunk_stage = DocumentStage.objects.get(
            document=self.document, name=document_services.STAGE_CHUNK_TEXT
        )
        self.assertEqual(chunk_stage.status, DocumentStage.STATUS_COMPLETED)
        self.assertTrue(
            document_services._stage_is_completed(
                self.document, document_services.STAGE_CHUNK_TEXT
            )
        )

    def test_reprocess_reset_clears_every_stage_for_a_clean_restart(self):
        """A genuine reprocess (reset_all_stages) must NOT preserve any stage.

        The admin ``_reset_for_reprocess`` calls ``reset_all_stages`` before a
        re-dispatch so the whole pipeline re-runs. Verify that even a COMPLETED
        stage is re-armed to PENDING, which is the behaviour that distinguishes
        reprocess from resume.
        """
        DocumentStage.objects.filter(document=self.document).update(
            status=DocumentStage.STATUS_COMPLETED
        )
        document_services.reset_all_stages(self.document)
        statuses = set(
            DocumentStage.objects.filter(document=self.document).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, {DocumentStage.STATUS_PENDING})

    def test_get_is_rejected(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 405)

    def test_non_failed_document_is_rejected(self):
        self.document.status = Document.STATUS_READY
        self.document.save(update_fields=["status"])
        with mock.patch(
            "documents.admin.enqueue_document_processing"
        ) as enqueue:
            resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 400)
        enqueue.assert_not_called()

    def test_out_of_scope_document_is_404(self):
        other_co = Company.objects.create(
            owner=self.other, name="OC", slug="oc"
        )
        foreign = Document.objects.create(
            company=other_co,
            uploaded_by=self.other,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_FAILED,
            title="foreign.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("foreign.pdf", b"x"),
        )
        staff = User.objects.create_user(
            email="staff@example.com", password="pw", is_staff=True
        )
        client = Client()
        client.force_login(staff)
        resp = client.post(self._url(pk=foreign.pk))
        self.assertEqual(resp.status_code, 404)


@override_settings(MEDIA_ROOT=_MEDIA)
class CancelEndpointTests(TestCase):
    """ITEM 3: the temporal_cancel admin endpoint (backend-aware stop)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="cadmin@example.com", password="pw"
        )
        self.other = User.objects.create_user(
            email="cother@example.com", password="x"
        )
        self.company = Company.objects.create(
            owner=self.admin, name="CC", slug="cc"
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def _doc(self, *, backend="temporal", status=Document.STATUS_PROCESSING):
        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            source=Document.SOURCE_LIBRARY,
            status=status,
            processing_backend=backend,
            processing_stage="extract_text",
            processing_progress=25,
            title="running.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("running.pdf", b"%PDF-1.4 fake"),
        )
        document_services._ensure_stages(document)
        DocumentStage.objects.filter(
            document=document, name=document_services.STAGE_MARK_PROCESSING
        ).update(status=DocumentStage.STATUS_COMPLETED)
        chunk = DocumentChunk.objects.create(
            company=self.company,
            document=document,
            chunk_index=0,
            page_number=1,
            content="kept",
        )
        return document, chunk

    def _url(self, pk):
        return reverse(
            "admin:documents_document_temporal_cancel", args=[pk]
        )

    def test_temporal_running_doc_terminates_workflow_and_stops(self):
        document, chunk = self._doc(backend="temporal")
        with mock.patch("documents.admin.TemporalProcessingBackend") as Backend:
            instance = Backend.return_value
            instance.terminate.return_value = {"terminated": True}
            resp = self.client.post(self._url(document.pk))

        self.assertEqual(resp.status_code, 200)
        instance.terminate.assert_called_once_with(f"doc-{document.pk}")
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_FAILED)
        self.assertEqual(
            document.processing_error, "Processing stopped by user."
        )
        # Resume-safe: completed stage + chunk preserved.
        mark_stage = DocumentStage.objects.get(
            document=document, name=document_services.STAGE_MARK_PROCESSING
        )
        self.assertEqual(mark_stage.status, DocumentStage.STATUS_COMPLETED)
        self.assertTrue(DocumentChunk.objects.filter(pk=chunk.pk).exists())

    def test_non_temporal_doc_flips_without_claiming_worker_kill(self):
        document, _chunk = self._doc(
            backend="celery", status=Document.STATUS_QUEUED
        )
        with mock.patch("documents.admin.TemporalProcessingBackend") as Backend:
            resp = self.client.post(self._url(document.pk))

        self.assertEqual(resp.status_code, 200)
        Backend.assert_not_called()
        data = resp.json()
        self.assertNotIn("terminated", data["message"].lower())
        document.refresh_from_db()
        self.assertEqual(document.status, Document.STATUS_FAILED)

    def test_ready_document_cannot_be_stopped(self):
        document, _chunk = self._doc(status=Document.STATUS_READY)
        resp = self.client.post(self._url(document.pk))
        self.assertEqual(resp.status_code, 400)

    def test_failed_document_cannot_be_stopped(self):
        document, _chunk = self._doc(status=Document.STATUS_FAILED)
        resp = self.client.post(self._url(document.pk))
        self.assertEqual(resp.status_code, 400)

    def test_get_is_rejected(self):
        document, _chunk = self._doc()
        resp = self.client.get(self._url(document.pk))
        self.assertEqual(resp.status_code, 405)

    def test_out_of_scope_document_is_404(self):
        other_co = Company.objects.create(
            owner=self.other, name="OCC", slug="occ"
        )
        foreign = Document.objects.create(
            company=other_co,
            uploaded_by=self.other,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_PROCESSING,
            processing_backend="temporal",
            title="foreign.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("foreign.pdf", b"x"),
        )
        staff = User.objects.create_user(
            email="cstaff@example.com", password="pw", is_staff=True
        )
        client = Client()
        client.force_login(staff)
        resp = client.post(self._url(foreign.pk))
        self.assertEqual(resp.status_code, 404)


class TerminateBackendTests(TestCase):
    """ITEM 3: TemporalProcessingBackend.terminate is tolerant (never raises)."""

    def test_terminate_returns_dict_and_swallows_client_errors(self):
        from infrastructure.processing.temporal_backend import (
            TemporalProcessingBackend,
        )

        backend = TemporalProcessingBackend()

        # Client.connect raising (server unreachable) → tolerant degraded dict.
        with mock.patch(
            "temporalio.client.Client.connect",
            side_effect=RuntimeError("no server"),
        ):
            result = backend.terminate("doc-abc")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("terminated"))
        self.assertTrue(result.get("degraded"))

    def test_terminate_calls_handle_terminate_on_success(self):
        from infrastructure.processing.temporal_backend import (
            TemporalProcessingBackend,
        )

        backend = TemporalProcessingBackend()

        handle = mock.Mock()
        handle.terminate = mock.AsyncMock(return_value=None)
        fake_client = mock.Mock()
        fake_client.get_workflow_handle.return_value = handle

        with mock.patch(
            "temporalio.client.Client.connect",
            new=mock.AsyncMock(return_value=fake_client),
        ):
            result = backend.terminate("doc-xyz")

        fake_client.get_workflow_handle.assert_called_once_with("doc-xyz")
        handle.terminate.assert_awaited_once()
        self.assertTrue(result.get("terminated"))


@override_settings(MEDIA_ROOT=_MEDIA)
class ChangelistRendersControlsTests(TestCase):
    """The changelist renders the Retry/Stop controls with the right data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="cl@example.com", password="pw"
        )
        self.company = Company.objects.create(
            owner=self.admin, name="CL", slug="cl"
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def _make(self, status):
        return Document.objects.create(
            company=self.company,
            uploaded_by=self.admin,
            source=Document.SOURCE_LIBRARY,
            status=status,
            processing_backend="temporal",
            title=f"{status}.pdf",
            file_type="pdf",
            file=SimpleUploadedFile(f"{status}.pdf", b"x"),
        )

    def test_changelist_contains_retry_and_stop_buttons(self):
        failed = self._make(Document.STATUS_FAILED)
        processing = self._make(Document.STATUS_PROCESSING)
        resp = self.client.get(
            reverse("admin:documents_document_changelist")
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("retry-action", html)
        self.assertIn("stop-action", html)
        self.assertIn(str(failed.pk), html)
        self.assertIn(str(processing.pk), html)
        # The controls root + script are wired into the page.
        self.assertIn("documents-controls-root", html)
