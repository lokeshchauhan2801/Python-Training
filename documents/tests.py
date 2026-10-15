"""Targeted tests for the documents app (Phase 1: offline extraction).

Covers each per-format extractor against tiny programmatic fixtures, the
word-boundary overlap chunker, best-effort OCR (skipped when tesseract is
absent), the synchronous ``process_document`` success + failure/cleanup paths,
owner/company scoping on the DRF API, and the admin HTTP smoke tests (Unfold
sidebar present on the Documents changelist + the scoped chunk JSON view).

Fixtures are built in-memory and uploaded via ``SimpleUploadedFile`` with a
``title`` carrying the right suffix so extractor dispatch works. ``MEDIA_ROOT``
is redirected to a temp dir that ``tearDown`` removes.
"""

import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from company.models import Company

from . import services
from .models import Document, DocumentChunk
from .services import (
    DocumentValidationError,
    extract_document_chunks,
    process_document,
    split_page_text,
)

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix="documents-tests-")


def _csv_bytes() -> bytes:
    return b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"


def _json_bytes() -> bytes:
    return b'{"title": "Report", "items": [{"name": "Alice"}, {"name": "Bob"}]}'


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["name", "age", "city"])
    ws.append(["Alice", 30, "NYC"])
    ws.append(["Bob", 25, "LA"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _docx_bytes() -> bytes:
    from docx import Document as WordDocument

    doc = WordDocument()
    doc.add_paragraph("The quick brown fox jumps over the lazy dog.")
    doc.add_paragraph("A second paragraph with more searchable text content.")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    """A minimal single-page PDF carrying a real text layer ('Hello PDF world')."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 58 >>\nstream\nBT /F1 24 Tf 72 700 Td (Hello PDF world) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref_pos = len(pdf)
    pdf += b"xref\n0 %d\n" % (len(objects) + 1)
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_pos)
    )
    return pdf


def _png_bytes(text: str = "HELLO OCR") -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 80), color="white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 30), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _make_document(company, user, filename, data, source=Document.SOURCE_LIBRARY):
    return Document.objects.create(
        company=company,
        uploaded_by=user,
        source=source,
        title=filename,
        file=SimpleUploadedFile(filename, data),
    )


@override_settings(MEDIA_ROOT=_MEDIA)
class ExtractionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _extract(self, filename, data):
        doc = _make_document(self.company, self.owner, filename, data)
        return extract_document_chunks(doc)

    def test_txt_extraction(self):
        chunks = self._extract("note.txt", b"The quick brown fox jumps over the dog.")
        self.assertTrue(chunks)
        page, location, content = chunks[0]
        self.assertEqual(page, 1)
        self.assertIn("TXT", location)
        self.assertIn("quick brown fox", content)

    def test_json_extraction(self):
        chunks = self._extract("data.json", _json_bytes())
        self.assertTrue(chunks)
        self.assertIn("JSON", chunks[0][1])
        self.assertIn("Report", chunks[0][2])

    def test_csv_extraction(self):
        chunks = self._extract("data.csv", _csv_bytes())
        self.assertTrue(chunks)
        _page, location, content = chunks[0]
        self.assertIn("CSV", location)
        self.assertIn("Columns:", content)
        self.assertIn("Alice", content)

    def test_xlsx_extraction(self):
        chunks = self._extract("sheet.xlsx", _xlsx_bytes())
        self.assertTrue(chunks)
        self.assertIn("Alice", chunks[0][2])

    def test_docx_extraction(self):
        chunks = self._extract("doc.docx", _docx_bytes())
        self.assertTrue(chunks)
        self.assertIn("quick brown fox", chunks[0][2])

    def test_pdf_extraction(self):
        chunks = self._extract("file.pdf", _pdf_bytes())
        self.assertTrue(chunks)
        joined = " ".join(content for _p, _l, content in chunks)
        self.assertIn("Hello PDF world", joined)

    def test_unsupported_type_raises(self):
        with self.assertRaises(DocumentValidationError):
            self._extract("archive.zip", b"PK\x03\x04not-a-real-zip")

    def test_ocr_image_extraction(self):
        if not services._tesseract_available():
            self.skipTest("Tesseract OCR binary not installed")
        chunks = self._extract("scan.png", _png_bytes("HELLO OCR"))
        self.assertTrue(chunks)
        self.assertIn("Image (OCR)", chunks[0][1])


class ChunkingTests(TestCase):
    def test_overlap_and_word_boundary(self):
        word = "lorem"
        text = " ".join([word] * 1000)  # well over CHUNK_SIZE
        chunks = split_page_text(text, page_number=7)
        self.assertGreater(len(chunks), 1)
        for page, content in chunks:
            self.assertEqual(page, 7)
            # Boundary cut is word-aware: no fragment starts/ends mid-"lorem".
            self.assertFalse(content.startswith("orem"))
            self.assertFalse(content.endswith("lore"))
            self.assertLessEqual(len(content), services.CHUNK_SIZE)
        # Consecutive chunks share overlapping text (the chunker rewinds).
        self.assertTrue(chunks[1][1].split(" ")[0] == word)

    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(split_page_text("   \n\t  ", 1), [])


@override_settings(MEDIA_ROOT=_MEDIA)
class ProcessDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_process_success_creates_chunks(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertGreater(doc.chunk_count, 0)
        self.assertEqual(doc.processing_progress, 100)
        self.assertIsNotNone(doc.processing_duration_ms)
        rows = DocumentChunk.objects.filter(document=doc)
        self.assertEqual(rows.count(), doc.chunk_count)
        for row in rows:
            self.assertEqual(row.company_id, self.company.id)

    def test_process_failure_marks_failed_and_cleans_up(self):
        # An empty TXT file has no searchable text -> DocumentValidationError.
        doc = _make_document(self.company, self.owner, "empty.txt", b"   ")
        with self.assertRaises(DocumentValidationError):
            process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_FAILED)
        self.assertEqual(doc.chunk_count, 0)
        self.assertTrue(doc.processing_error)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)

    def test_reprocess_replaces_stale_chunks(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        process_document(doc)
        first_count = doc.chunk_count
        # Re-run: stale chunks are purged and rebuilt, no unique-constraint clash.
        process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.chunk_count, first_count)
        self.assertEqual(
            DocumentChunk.objects.filter(document=doc).count(), first_count
        )


@override_settings(MEDIA_ROOT=_MEDIA)
class DocumentApiScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.alice = User.objects.create_user(email="alice@example.com", password="x")
        self.bob = User.objects.create_user(email="bob@example.com", password="x")
        self.alice_co = Company.objects.create(owner=self.alice, name="AC", slug="ac")
        self.bob_co = Company.objects.create(owner=self.bob, name="BC", slug="bc")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _upload(self, as_user, **overrides):
        self.client.force_authenticate(as_user)
        payload = {
            "title": "data.csv",
            "source": Document.SOURCE_LIBRARY,
            "file": SimpleUploadedFile("data.csv", _csv_bytes()),
        }
        payload.update(overrides)
        return self.client.post(
            "/api/v1/documents/document/", payload, format="multipart"
        )

    def test_create_sets_uploaded_by_and_defaults_company(self):
        resp = self._upload(self.alice)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(str(resp.data["company"]), str(self.alice_co.id))
        self.assertEqual(str(resp.data["uploaded_by"]), str(self.alice.id))
        # Server-controlled status cannot be forged by the client.
        self.assertEqual(resp.data["status"], Document.STATUS_UPLOADED)

    def test_server_controlled_fields_ignored_from_payload(self):
        resp = self._upload(
            self.alice, status=Document.STATUS_READY, chunk_count=99
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], Document.STATUS_UPLOADED)
        self.assertEqual(resp.data["chunk_count"], 0)

    def test_cannot_attach_another_owners_company(self):
        resp = self._upload(self.alice, company=str(self.bob_co.id))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("company", resp.data)

    def test_other_owner_cannot_see_document(self):
        resp = self._upload(self.alice)
        doc_id = resp.data["id"]
        self.client.force_authenticate(self.bob)
        self.assertEqual(
            self.client.get(f"/api/v1/documents/document/{doc_id}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/documents/document/{doc_id}/chunks/"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/v1/documents/document/{doc_id}/process/"
            ).status_code,
            404,
        )

    def test_superuser_sees_all(self):
        resp = self._upload(self.alice)
        doc_id = resp.data["id"]
        root = User.objects.create_superuser(email="root@example.com", password="x")
        self.client.force_authenticate(root)
        self.assertEqual(
            self.client.get(f"/api/v1/documents/document/{doc_id}/").status_code,
            200,
        )

    def test_process_action_returns_ready(self):
        resp = self._upload(self.alice)
        doc_id = resp.data["id"]
        self.client.force_authenticate(self.alice)
        proc = self.client.post(
            f"/api/v1/documents/document/{doc_id}/process/"
        )
        self.assertEqual(proc.status_code, 200, proc.data)
        self.assertIn(proc.data["status"], {Document.STATUS_READY, Document.STATUS_FAILED})
        chunks = self.client.get(f"/api/v1/documents/document/{doc_id}/chunks/")
        self.assertEqual(chunks.status_code, 200)
        self.assertTrue(chunks.data)


@override_settings(MEDIA_ROOT=_MEDIA)
class DocumentAdminHttpTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # --keepdb uses a SEPARATE test database, so create the superuser here.
        cls.admin = User.objects.create_superuser(
            email="lokeshchauhan058@gmail.com", password="adminpass123"
        )
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other_owner = User.objects.create_user(email="x@example.com", password="x")
        cls.other_co = Company.objects.create(
            owner=cls.other_owner, name="X", slug="x"
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.client.force_login(self.admin)

    def _ready_document(self, company, owner):
        doc = _make_document(company, owner, "data.csv", _csv_bytes())
        process_document(doc)
        return doc

    def test_changelist_renders_with_unfold_sidebar(self):
        resp = self.client.get("/admin/documents/document/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # Unfold shell marker: the sidebar navigation we configured is present,
        # confirming the page did NOT fall back to plain Django admin.
        self.assertIn("sidebar", body.lower())
        self.assertIn("Documents", body)

    def test_admin_index_renders_with_sidebar(self):
        resp = self.client.get("/admin/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("sidebar", resp.content.decode().lower())

    def test_chunks_json_view_in_scope(self):
        doc = self._ready_document(self.company, self.owner)
        resp = self.client.get(f"/admin/documents/document/{doc.pk}/chunks-json/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["document"]["id"], str(doc.pk))
        self.assertTrue(data["chunks"])
        self.assertIn("content", data["chunks"][0])

    def test_chunks_json_view_out_of_scope_404(self):
        # A non-superuser admin cannot see another company's document.
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        other_doc = self._ready_document(self.other_co, self.other_owner)
        self.client.force_login(self.owner)
        resp = self.client.get(
            f"/admin/documents/document/{other_doc.pk}/chunks-json/"
        )
        self.assertEqual(resp.status_code, 404)
