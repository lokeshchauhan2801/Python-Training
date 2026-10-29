import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from company.models import Company
from documents import services
from documents.models import Document, DocumentChunk, DocumentStage

User = get_user_model()
_MEDIA = tempfile.mkdtemp(prefix="documents-extraction-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class PdfExtractionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(email="extract@example.com", password="x")
        self.company = Company.objects.create(owner=self.owner, name="Extract", slug="extract")
        self.document = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="scan.pdf",
            file_type="pdf",
            file=SimpleUploadedFile("scan.pdf", b"fake-pdf"),
        )

    def _reader(self, texts):
        pages = []
        for text in texts:
            page = mock.Mock()
            page.extract_text.return_value = text
            pages.append(page)
        reader = mock.Mock()
        reader.pages = pages
        return reader

    def test_text_layer_pdf_bypasses_ocr(self):
        with mock.patch("documents.services.PdfReader", return_value=self._reader(["selectable text"])):
            with mock.patch("documents.services._ocr_pdf") as ocr:
                chunks = services._extract_pdf(self.document)
        self.assertEqual(chunks[0][2], "selectable text")
        ocr.assert_not_called()

    @override_settings(DOCUMENT_OCR_BATCH_PAGES=1, DOCUMENT_OCR_PDF_DPI=300)
    def test_scanned_pdf_ocr_is_sequential_bounded_and_closes_images(self):
        images = [mock.Mock(), mock.Mock()]
        progress = []

        def render(_raw, **kwargs):
            self.assertEqual(kwargs["dpi"], 300)
            self.assertEqual(kwargs["thread_count"], 1)
            self.assertEqual(kwargs["last_page"], kwargs["first_page"])
            return [images[kwargs["first_page"] - 1]]

        with mock.patch("documents.services.PdfReader", return_value=self._reader(["", ""])):
            with mock.patch("documents.services._tesseract_available", return_value=True):
                with mock.patch("documents.services.convert_from_bytes", side_effect=render) as convert:
                    with mock.patch("documents.services._ocr_image", side_effect=["first", "second"]):
                        chunks = services._extract_pdf(
                            self.document,
                            progress_callback=lambda page, total: progress.append((page, total)),
                        )
        self.assertEqual([chunk[0] for chunk in chunks], [1, 2])
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual(convert.call_count, 2)
        for image in images:
            image.close.assert_called_once_with()

    @override_settings(DOCUMENT_MAX_PDF_BYTES=3)
    def test_pdf_over_byte_limit_fails_before_rasterization(self):
        with mock.patch("documents.services.PdfReader", return_value=self._reader([""])):
            with mock.patch("documents.services.convert_from_bytes") as convert:
                with self.assertRaisesRegex(services.DocumentValidationError, "too large"):
                    services._extract_pdf(self.document)
        convert.assert_not_called()

    @override_settings(DOCUMENT_MAX_PDF_PAGES=1)
    def test_pdf_over_page_limit_fails_before_rasterization(self):
        with mock.patch("documents.services.PdfReader", return_value=self._reader(["", ""])):
            with mock.patch("documents.services.convert_from_bytes") as convert:
                with self.assertRaisesRegex(services.DocumentValidationError, "too many pages"):
                    services._extract_pdf(self.document)
        convert.assert_not_called()

    def test_render_timeout_is_actionable_validation_failure(self):
        with mock.patch("documents.services.PdfReader", return_value=self._reader([""])):
            with mock.patch("documents.services._tesseract_available", return_value=True):
                with mock.patch("documents.services.convert_from_bytes", side_effect=RuntimeError("timeout")):
                    with self.assertRaisesRegex(services.DocumentValidationError, "rendering timed out"):
                        services._extract_pdf(self.document)


@override_settings(MEDIA_ROOT=_MEDIA)
class ExtractStageProgressTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user(email="stage@example.com", password="x")
        self.company = Company.objects.create(owner=self.owner, name="Stage", slug="stage")
        self.document = Document.objects.create(
            company=self.company, uploaded_by=self.owner, source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_PROCESSING, title="scan.pdf", file_type="pdf",
            file=SimpleUploadedFile("scan.pdf", b"fake-pdf"),
        )

    def test_ocr_callback_persists_page_detail_and_finishes_at_thirty_percent(self):
        def extract(_document, ocr_progress_callback=None):
            ocr_progress_callback(1, 2)
            ocr_progress_callback(2, 2)
            return [(1, "Page 1 (OCR)", "text")]

        with mock.patch("documents.services.extract_document_chunks", side_effect=extract):
            services.stage_extract_text(str(self.document.pk))

        self.document.refresh_from_db()
        stage = DocumentStage.objects.get(document=self.document, name=services.STAGE_EXTRACT_TEXT)
        self.assertEqual(self.document.processing_progress, 30)
        self.assertIn("OCR page 2/2", stage.detail)
        self.assertEqual(stage.status, DocumentStage.STATUS_COMPLETED)

    def test_completed_terminal_stage_repairs_non_ready_document(self):
        services._ensure_stages(self.document)
        DocumentStage.objects.filter(document=self.document, name=services.STAGE_MARK_COMPLETED).update(
            status=DocumentStage.STATUS_COMPLETED
        )
        DocumentChunk.objects.create(
            company=self.company, document=self.document, chunk_index=0,
            page_number=1, source_location="Page 1", content="text",
        )
        services.stage_mark_completed(str(self.document.pk))
        self.document.refresh_from_db()
        self.assertEqual(self.document.status, Document.STATUS_READY)


@override_settings(MEDIA_ROOT=_MEDIA)
class TemporalInfoSnapshotTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.addClassCleanup(shutil.rmtree, _MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_superuser(email="info@example.com", password="x")
        self.company = Company.objects.create(owner=self.owner, name="Info", slug="info")
        self.document = Document.objects.create(
            company=self.company, uploaded_by=self.owner, source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_PROCESSING, processing_backend="temporal",
            processing_stage="extract_text", processing_progress=25, processing_attempts=2,
            processing_task_id="task-1", title="info.pdf", file_type="pdf",
            file=SimpleUploadedFile("info.pdf", b"fake-pdf"),
        )
        services._ensure_stages(self.document)
        stage = DocumentStage.objects.get(
            document=self.document, name=services.STAGE_EXTRACT_TEXT
        )
        stage.status = DocumentStage.STATUS_RUNNING
        stage.started_at = timezone.now() - timezone.timedelta(seconds=1)
        stage.save(update_fields=["status", "started_at"])
        self.client = Client()
        self.client.force_login(self.owner)

    def test_info_returns_db_and_temporal_snapshots_with_mismatch_warning(self):
        status = {
            "status": "COMPLETED", "run_id": "run-1", "started": "2025-01-01T00:00:00+00:00",
            "closed": "2025-01-01T00:01:00+00:00", "result": None, "degraded": False,
        }
        url = reverse("admin:documents_document_temporal_info", args=[self.document.pk])
        with mock.patch("documents.admin.TemporalProcessingBackend") as Backend:
            Backend.return_value.get_status.return_value = status
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        Backend.return_value.get_status.assert_called_once_with(f"doc-{self.document.pk}")
        self.assertEqual(payload["db"]["status"], Document.STATUS_PROCESSING)
        self.assertEqual(payload["temporal"]["run_id"], "run-1")
        self.assertTrue(payload["temporal_url"])
        self.assertIsNotNone(payload["stages"][1]["elapsed_ms"])
        self.assertIn("do not agree", " ".join(payload["warnings"]))
        self.assertTrue(payload["polling"])

    def test_status_json_does_not_query_temporal_and_calls_ready_completed(self):
        self.document.status = Document.STATUS_READY
        self.document.save(update_fields=["status"])
        url = reverse("admin:documents_document_status_json")
        with mock.patch("documents.admin.TemporalProcessingBackend") as Backend:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        Backend.assert_not_called()
        row = next(item for item in response.json()["documents"] if item["id"] == str(self.document.pk))
        self.assertEqual(row["status_display"], "Completed")
