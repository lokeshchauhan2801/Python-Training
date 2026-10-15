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
    extract_document_text,
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


# ---------------------------------------------------------------------------
# Shared embedding/vector test harness.
#
# ANY test that runs process_document on a LIBRARY document (Phase 1 OR Phase 2)
# now embeds + writes vectors, so it MUST use a FAKE embeddings client (no
# network, no live key) AND a per-test tmp CHROMA_PATH (never the real
# chroma_data). These helpers are defined up here so the Phase-1 process/admin/
# API suites can reuse them too.
# ---------------------------------------------------------------------------

from unittest import mock  # noqa: E402

from django.urls import reverse  # noqa: E402

from basics.llm import LLMConfigurationError  # noqa: E402
from basics.vectorstore import ChromaVectorStore  # noqa: E402

from .services import VECTOR_COLLECTION, find_relevant_chunks  # noqa: E402


class _FakeEmbeddings:
    """Deterministic 8-dim embeddings keyed off the text hash (no network)."""

    DIM = 8

    def _vector(self, text):
        seed = abs(hash(text))
        return [((seed >> (i * 3)) % 97) / 97.0 for i in range(self.DIM)]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


class _FakeLLMClient:
    """Stand-in for the LLM seam exposing only the embedding methods."""

    def __init__(self, embeddings=None, *, raise_no_key=False):
        self._embeddings = embeddings or _FakeEmbeddings()
        self._raise_no_key = raise_no_key

    def _check(self):
        if self._raise_no_key:
            raise LLMConfigurationError("LLM API key required to embed documents.")

    def embed_documents(self, texts):
        self._check()
        return self._embeddings.embed_documents(texts)

    def embed_query(self, text):
        self._check()
        return self._embeddings.embed_query(text)


def _fake_client_factory(embeddings=None, *, raise_no_key=False):
    client = _FakeLLMClient(embeddings, raise_no_key=raise_no_key)
    return lambda: client


class _TmpChromaMixin:
    """Give each test an isolated tmp CHROMA_PATH, removed on tearDown."""

    def setUp(self):
        super().setUp()
        self._chroma_dir = tempfile.mkdtemp(prefix="documents-chroma-")
        self._chroma_override = override_settings(CHROMA_PATH=self._chroma_dir)
        self._chroma_override.enable()

    def tearDown(self):
        self._chroma_override.disable()
        shutil.rmtree(self._chroma_dir, ignore_errors=True)
        super().tearDown()


class _FakeLLMMixin(_TmpChromaMixin):
    """Tmp CHROMA_PATH + a fake LLM client patched into documents.services.

    Any suite that calls process_document on a LIBRARY document mixes this in so
    embedding never hits the network / a live key and vectors land only in the
    per-test tmp Chroma dir.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        )
        patcher.start()
        self.addCleanup(patcher.stop)


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

    def test_extract_document_text_joins_txt(self):
        doc = _make_document(
            self.company,
            self.owner,
            "note.txt",
            b"The capital of France is Paris.",
        )
        text = extract_document_text(doc)
        self.assertIn("The capital of France is Paris.", text)

    def test_extract_document_text_includes_csv_values(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        text = extract_document_text(doc)
        self.assertIn("Alice", text)

    def test_extract_document_text_returns_empty_on_validation_error(self):
        # An unsupported type raises DocumentValidationError inside the chunk
        # extractor; extract_document_text swallows it and returns "".
        doc = _make_document(
            self.company, self.owner, "archive.zip", b"PK\x03\x04not-a-real-zip"
        )
        self.assertEqual(extract_document_text(doc), "")


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
class ProcessDocumentTests(_FakeLLMMixin, TestCase):
    # process_document on a LIBRARY doc now embeds, so this suite uses the fake
    # LLM client + tmp CHROMA_PATH harness (no network, no live key, no real
    # chroma_data writes).
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
class DocumentApiScopingTests(_FakeLLMMixin, TestCase):
    # The process action embeds library docs, so use the fake LLM + tmp Chroma
    # harness (no network, no live key, no real chroma_data writes).
    def setUp(self):
        super().setUp()
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
class DocumentAdminHttpTests(_FakeLLMMixin, TestCase):
    # _ready_document processes LIBRARY docs (now embeds), so use the fake LLM +
    # tmp Chroma harness (no network, no live key, no real chroma_data writes).
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
        super().setUp()
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

    def test_chunks_html_view_renders_with_unfold_sidebar(self):
        # The "Show" pill opens an HTML page (NOT raw JSON) that keeps the
        # Unfold left sidebar; it fetches the JSON endpoint as its data source.
        doc = self._ready_document(self.company, self.owner)
        resp = self.client.get(f"/admin/documents/document/{doc.pk}/chunks/")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # Unfold shell marker + the JSON data-source URL embedded for the fetch.
        self.assertIn("sidebar", body.lower())
        self.assertIn(f"/admin/documents/document/{doc.pk}/chunks-json/", body)

    def test_show_pill_redirects_to_html_viewer(self):
        # The row-action pill must point at the HTML viewer, not the raw JSON.
        doc = self._ready_document(self.company, self.owner)
        resp = self.client.get(
            f"/admin/documents/document/{doc.pk}/show-chunks/"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/admin/documents/document/{doc.pk}/chunks/")

    def test_chunks_html_view_out_of_scope_404(self):
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        other_doc = self._ready_document(self.other_co, self.other_owner)
        self.client.force_login(self.owner)
        resp = self.client.get(
            f"/admin/documents/document/{other_doc.pk}/chunks/"
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Phase 2 (embeddings + RAG) tests
# ---------------------------------------------------------------------------
#
# Every test here sets CHROMA_PATH to a per-test tmp dir (override_settings +
# tearDown rmtree) and uses a FAKE embeddings client (deterministic vectors) —
# NEVER touching the real chroma_data and NEVER requiring a live key. The
# fake-client / tmp-Chroma harness is defined near the top of this module.


@override_settings(MEDIA_ROOT=_MEDIA)
class VectorStoreTests(_TmpChromaMixin, TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_add_similarity_get_delete_round_trip(self):
        store = ChromaVectorStore()
        docs = [
            {
                "id": "a",
                "text": "alpha",
                "embedding": [1.0, 0.0, 0.0],
                "metadata": {"company_id": "c1"},
            },
            {
                "id": "b",
                "text": "beta",
                "embedding": [0.0, 1.0, 0.0],
                "metadata": {"company_id": "c1"},
            },
        ]
        ids = store.add_documents(docs, collection=VECTOR_COLLECTION)
        self.assertEqual(set(ids), {"a", "b"})

        hits = store.similarity_search(
            [1.0, 0.0, 0.0], collection=VECTOR_COLLECTION, k=2
        )
        self.assertEqual(hits[0]["id"], "a")

        got = store.get_embeddings(["a"], collection=VECTOR_COLLECTION)
        self.assertEqual(len(got["a"]), 3)

        store.delete_documents(["a", "b"], collection=VECTOR_COLLECTION)
        self.assertEqual(
            store.similarity_search(
                [1.0, 0.0, 0.0], collection=VECTOR_COLLECTION, k=2
            ),
            [],
        )

    def test_missing_collection_search_returns_empty_not_error(self):
        """A read against a never-created collection is a NORMAL zero-hit case.

        With a fresh (empty) CHROMA_PATH the collection does not exist yet;
        similarity_search / get_embeddings must return empty rather than raising
        InvalidCollectionException (so chat still answers with no documents).
        """
        store = ChromaVectorStore()
        self.assertEqual(
            store.similarity_search(
                [1.0, 0.0, 0.0], collection="never_created_collection", k=5
            ),
            [],
        )
        self.assertEqual(
            store.get_embeddings(["x"], collection="never_created_collection"),
            {},
        )


class LLMSeamTests(_TmpChromaMixin, TestCase):
    def test_fake_backend_embeds(self):
        client = _FakeLLMClient()
        vectors = client.embed_documents(["one", "two"])
        self.assertEqual(len(vectors), 2)
        self.assertEqual(len(vectors[0]), _FakeEmbeddings.DIM)
        self.assertEqual(len(client.embed_query("one")), _FakeEmbeddings.DIM)

    def test_no_key_raises_configuration_error(self):
        client = _FakeLLMClient(raise_no_key=True)
        with self.assertRaises(LLMConfigurationError):
            client.embed_documents(["x"])
        with self.assertRaises(LLMConfigurationError):
            client.embed_query("x")

    def test_real_seam_embeds_require_a_key(self):
        # The real LLMClient raises LLMConfigurationError when no key is set
        # (import-safe; builds no backend). Guard against a key leaking in from
        # the shell by only asserting when none is configured.
        from django.conf import settings

        if settings.LLM_API_KEY:
            self.skipTest("A live LLM key is configured in this environment")
        from basics.llm import get_llm_client

        with self.assertRaises(LLMConfigurationError):
            get_llm_client().embed_query("hello")


@override_settings(MEDIA_ROOT=_MEDIA)
class ProcessDocumentEmbeddingTests(_TmpChromaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_library_doc_embeds_and_marks_ready(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        with mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        ):
            process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertGreater(doc.chunk_count, 0)
        # Every chunk vector is written to Chroma, keyed by the chunk ULID.
        chunk_ids = [
            str(pk)
            for pk in DocumentChunk.objects.filter(document=doc).values_list(
                "id", flat=True
            )
        ]
        stored = ChromaVectorStore().get_embeddings(
            chunk_ids, collection=VECTOR_COLLECTION
        )
        self.assertEqual(set(stored), set(chunk_ids))

    def test_chat_doc_is_not_embedded(self):
        doc = _make_document(
            self.company,
            self.owner,
            "note.txt",
            b"The quick brown fox jumps over the dog.",
            source=Document.SOURCE_CHAT,
        )
        # get_llm_client must never be called for a chat-source document.
        sentinel = mock.Mock(side_effect=AssertionError("chat doc was embedded"))
        with mock.patch.object(services, "get_llm_client", sentinel):
            process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertGreater(doc.chunk_count, 0)
        chunk_ids = [
            str(pk)
            for pk in DocumentChunk.objects.filter(document=doc).values_list(
                "id", flat=True
            )
        ]
        # No vectors were written for a chat doc. Either the 'pdf_chunks'
        # collection was never created (chat was the only doc processed) or it
        # exists without these chunk ids — both prove the chat doc is unembedded.
        try:
            stored = ChromaVectorStore().get_embeddings(
                chunk_ids, collection=VECTOR_COLLECTION
            )
        except Exception:
            stored = {}
        self.assertEqual(stored, {})

    def test_embed_failure_cleans_vectors_and_chunks(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())

        class _Boom(_FakeLLMClient):
            def embed_documents(self, texts):
                raise RuntimeError("provider exploded")

        with mock.patch.object(
            services, "get_llm_client", lambda: _Boom()
        ):
            with self.assertRaises(services.DocumentIndexError):
                process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_FAILED)
        self.assertEqual(doc.chunk_count, 0)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)

    def test_no_key_marks_failed_with_message_and_no_partial_state(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        with mock.patch.object(
            services,
            "get_llm_client",
            _fake_client_factory(raise_no_key=True),
        ):
            with self.assertRaises(services.DocumentIndexError) as ctx:
                process_document(doc)
        self.assertIn("LLM key required", str(ctx.exception))
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_FAILED)
        self.assertEqual(doc.chunk_count, 0)
        self.assertEqual(DocumentChunk.objects.filter(document=doc).count(), 0)
        self.assertIn("LLM key required", doc.processing_error)


@override_settings(MEDIA_ROOT=_MEDIA)
class RetrievalScopingTests(_TmpChromaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(email="alice@example.com", password="x")
        cls.bob = User.objects.create_user(email="bob@example.com", password="x")
        cls.alice_co = Company.objects.create(
            owner=cls.alice, name="AC", slug="ac"
        )
        cls.bob_co = Company.objects.create(owner=cls.bob, name="BC", slug="bc")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _process(self, company, owner, filename, data):
        doc = _make_document(company, owner, filename, data)
        with mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        ):
            process_document(doc)
        return doc

    def test_owner_only_retrieves_own_company_chunks(self):
        self._process(self.alice_co, self.alice, "a.csv", _csv_bytes())
        with mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        ):
            hits = find_relevant_chunks(self.alice, "Alice", self.alice_co, k=5)
        self.assertTrue(hits)
        for chunk in hits:
            self.assertEqual(chunk.company_id, self.alice_co.id)

    def test_other_owner_excluded_even_if_chroma_returns_ids(self):
        bob_doc = self._process(self.bob_co, self.bob, "b.csv", _csv_bytes())
        bob_chunk_ids = [
            str(pk)
            for pk in DocumentChunk.objects.filter(document=bob_doc).values_list(
                "id", flat=True
            )
        ]
        # Chroma returns Bob's chunk ids, but Alice does not own Bob's company,
        # so the DB re-scope (document__company__owner=alice) drops them all.
        fake_search = mock.Mock(
            return_value=[{"id": cid} for cid in bob_chunk_ids]
        )
        with mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        ), mock.patch.object(
            ChromaVectorStore, "similarity_search", fake_search
        ):
            hits = find_relevant_chunks(self.alice, "anything", self.bob_co, k=5)
        self.assertEqual(hits, [])


@override_settings(MEDIA_ROOT=_MEDIA)
class AdminEmbeddingColumnTests(_TmpChromaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            email="root-embed@example.com", password="x"
        )
        cls.member = User.objects.create_user(
            email="member@example.com", password="x", is_staff=True
        )
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _embedded_chunk(self):
        doc = _make_document(self.company, self.owner, "data.csv", _csv_bytes())
        with mock.patch.object(
            services, "get_llm_client", _fake_client_factory()
        ):
            process_document(doc)
        return DocumentChunk.objects.filter(document=doc).first()

    def _admin(self):
        from django.contrib.admin.sites import site

        from .admin import DocumentChunkAdmin

        return DocumentChunkAdmin(DocumentChunk, site)

    def _request(self, user):
        from django.test import RequestFactory

        request = RequestFactory().get("/admin/documents/documentchunk/")
        request.user = user
        return request

    def test_superuser_sees_truncated_preview(self):
        chunk = self._embedded_chunk()
        admin_obj = self._admin()
        admin_obj._request = self._request(self.admin)
        rendered = admin_obj.embedding_preview(chunk)
        self.assertTrue(rendered.startswith("["))
        self.assertIn("more", rendered)

    def test_non_superuser_sees_dash(self):
        chunk = self._embedded_chunk()
        admin_obj = self._admin()
        admin_obj._request = self._request(self.member)
        self.assertEqual(admin_obj.embedding_preview(chunk), "—")

    def test_non_embedded_chunk_sees_dash(self):
        # A chat-source chunk is never embedded -> get_embeddings returns {}.
        doc = _make_document(
            self.company,
            self.owner,
            "note.txt",
            b"hello there world",
            source=Document.SOURCE_CHAT,
        )
        process_document(doc)
        chunk = DocumentChunk.objects.filter(document=doc).first()
        admin_obj = self._admin()
        admin_obj._request = self._request(self.admin)
        self.assertEqual(admin_obj.embedding_preview(chunk), "—")


@override_settings(MEDIA_ROOT=_MEDIA)
class AdminScopingSecurityTests(_TmpChromaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            email="alice-admin@example.com", password="x", is_staff=True
        )
        cls.bob = User.objects.create_user(email="bob-admin@example.com", password="x")
        cls.root = User.objects.create_superuser(
            email="root-admin@example.com", password="x"
        )
        cls.alice_co = Company.objects.create(
            owner=cls.alice, name="AliceCo", slug="alice-co"
        )
        cls.bob_co = Company.objects.create(owner=cls.bob, name="BobCo", slug="bob-co")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _admin(self):
        from django.contrib.admin.sites import site

        from .admin import DocumentAdmin

        return DocumentAdmin(Document, site)

    def _request(self, user):
        from django.test import RequestFactory

        request = RequestFactory().post("/admin/documents/document/add/")
        request.user = user
        return request

    def test_company_choices_limited_to_owned_for_non_superuser(self):
        from company.models import Company

        admin_obj = self._admin()
        db_field = Document._meta.get_field("company")
        request = self._request(self.alice)
        formfield = admin_obj.formfield_for_foreignkey(db_field, request)
        choice_ids = {str(c.pk) for c in formfield.queryset}
        self.assertEqual(choice_ids, {str(self.alice_co.pk)})
        self.assertNotIn(str(self.bob_co.pk), choice_ids)
        # Superuser keeps the full queryset.
        root_formfield = admin_obj.formfield_for_foreignkey(
            db_field, self._request(self.root)
        )
        self.assertEqual(
            set(root_formfield.queryset), set(Company.objects.all())
        )

    def test_uploaded_by_is_readonly(self):
        admin_obj = self._admin()
        request = self._request(self.alice)
        self.assertIn("uploaded_by", admin_obj.get_readonly_fields(request))

    def test_save_auto_sets_uploader_and_coerces_tampered_company(self):
        admin_obj = self._admin()
        request = self._request(self.alice)
        # Build an unsaved library doc naming BOB's company + a bogus uploader.
        doc = Document(
            company=self.bob_co,
            uploaded_by=self.bob,
            source=Document.SOURCE_CHAT,  # chat so save_model skips extraction
            title="tamper.txt",
            file=SimpleUploadedFile("tamper.txt", b"hello there world"),
        )
        admin_obj.save_model(request, doc, form=None, change=False)
        doc.refresh_from_db()
        # Company coerced to Alice's owned company; uploader forced to Alice.
        self.assertEqual(doc.company_id, self.alice_co.id)
        self.assertEqual(doc.uploaded_by_id, self.alice.id)

    def test_superuser_may_pick_any_company(self):
        admin_obj = self._admin()
        request = self._request(self.root)
        doc = Document(
            company=self.bob_co,
            source=Document.SOURCE_CHAT,
            title="root.txt",
            file=SimpleUploadedFile("root.txt", b"hello there world"),
        )
        admin_obj.save_model(request, doc, form=None, change=False)
        doc.refresh_from_db()
        # Superuser's chosen company is respected (not coerced).
        self.assertEqual(doc.company_id, self.bob_co.id)
        # The uploader is still auto-stamped to the acting superuser.
        self.assertEqual(doc.uploaded_by_id, self.root.id)
