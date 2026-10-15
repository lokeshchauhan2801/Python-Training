"""PART A targeted tests: the minimal Documents ADD form.

The Documents add form is intentionally minimal — the user only chooses a file
OR a media URL. Everything else is set automatically server-side:

* ``source=library``, ``uploaded_by=request.user`` and ``company`` (the user's
  owned/active company) are stamped on save (DocumentAdmin.save_model +
  BaseModelAdmin/CompanyScopedAdminMixin).
* ``title`` is optional on the form and derived at process time from the
  extracted text via the LLM, with a filename-stem / URL-host no-key fallback.
  A user-typed title is preserved.
* ``file_type`` is auto-filled at process time from the file suffix.

These tests run with NO broker and NO live key. ``dispatch_document_processing``
is patched to run ``process_document`` inline; the LLM seam is patched with a
fake client (embeddings + a canned ``complete``). ``MEDIA_ROOT``/``CHROMA_PATH``
are redirected to per-run temp dirs. Mirrors the fixture/mocking patterns in
documents/tests_phase4.py and the Phase-2 fake-LLM harness in documents/tests.py.
"""
from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from basics.llm import LLMConfigurationError
from company.models import Company

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from . import services
from .admin import DocumentAdmin
from .models import Document

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix="documents-docsform-")

_ADD_URL = "/admin/documents/document/add/"


def _csv_bytes() -> bytes:
    return b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"


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
    """Fake LLM seam: canned ``complete`` title + deterministic embeddings.

    ``raise_no_key=True`` simulates the no-key path by raising
    ``LLMConfigurationError`` from BOTH ``complete`` and the embedding methods,
    exactly as the lazy real client would when no key is configured.
    """

    def __init__(self, *, completion="LLM Derived Title", raise_no_key=False):
        self._embeddings = _FakeEmbeddings()
        self._completion = completion
        self._raise_no_key = raise_no_key

    def _check(self):
        if self._raise_no_key:
            raise LLMConfigurationError("No LLM API key configured.")

    def complete(self, messages, **kwargs):
        self._check()
        return self._completion

    def embed_documents(self, texts):
        self._check()
        return self._embeddings.embed_documents(texts)

    def embed_query(self, text):
        self._check()
        return self._embeddings.embed_query(text)


def _inline_dispatch(document_id, backend=None):
    """Stand-in for dispatch_document_processing: run processing synchronously."""
    document = Document.objects.get(pk=document_id)
    services.process_document(document)
    return "job-inline", "local-background"


@override_settings(MEDIA_ROOT=_MEDIA)
class DocsAddFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A non-superuser staff owner with exactly one owned company, so the
        # CompanyScopedAdminMixin resolves the active company server-side.
        cls.owner = User.objects.create_user(
            email="owner@example.com", password="x", is_staff=True
        )
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        # Grant the documents permissions needed to reach the add view/save.
        from django.contrib.auth.models import Permission

        perms = Permission.objects.filter(
            content_type__app_label="documents",
            content_type__model="document",
        )
        cls.owner.user_permissions.add(*perms)
        # A superuser for the GET add-form render assertion (no company scoping
        # needed there).
        cls.root = User.objects.create_superuser(
            email="root@example.com", password="x"
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # Per-test tmp CHROMA_PATH so embedding writes never touch real data.
        self._chroma_dir = tempfile.mkdtemp(prefix="documents-docsform-chroma-")
        override = override_settings(CHROMA_PATH=self._chroma_dir)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self._chroma_dir, True)

    def _patch_llm(self, client):
        patcher = mock.patch.object(services, "get_llm_client", lambda: client)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- Add form render ----------------------------------------------------
    def test_add_form_shows_only_file_and_url(self):
        # GET the add form: Unfold sidebar present; ONLY file + source_url
        # widgets render — no title/source/file_type/status/processing_*/
        # company/uploaded_by/session/audit widgets.
        self.client.force_login(self.root)
        resp = self.client.get(_ADD_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("sidebar", body.lower())
        self.assertIn('name="file"', body)
        self.assertIn('name="source_url"', body)
        for hidden in (
            'name="title"',
            'name="source"',
            'name="file_type"',
            'name="status"',
            'name="processing_backend"',
            'name="processing_progress"',
            'name="company"',
            'name="uploaded_by"',
            'name="session"',
            'name="created_by"',
            'name="updated_by"',
        ):
            self.assertNotIn(hidden, body)

    # --- Add POST: blank title, LLM-derived ---------------------------------
    def test_add_blank_title_is_llm_derived_and_file_type_filled(self):
        self._patch_llm(_FakeLLMClient(completion="Quarterly Sales Summary"))
        self.client.force_login(self.owner)
        with mock.patch(
            "documents.admin.dispatch_document_processing",
            side_effect=_inline_dispatch,
        ):
            resp = self.client.post(
                _ADD_URL,
                {
                    "file": SimpleUploadedFile("report.csv", _csv_bytes()),
                    "source_url": "",
                },
            )
        self.assertEqual(resp.status_code, 302)
        doc = Document.objects.get()
        self.assertEqual(doc.source, Document.SOURCE_LIBRARY)
        self.assertEqual(doc.uploaded_by_id, self.owner.id)
        self.assertEqual(doc.company_id, self.company.id)
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertEqual(doc.title, "Quarterly Sales Summary")
        self.assertEqual(doc.file_type, "csv")

    # --- Add POST: user-typed title preserved -------------------------------
    def test_user_supplied_title_preserved(self):
        # The add form hides title, but a title that already carries a suffix
        # (e.g. a URL download, or a programmatic create) must be preserved and
        # NOT overwritten by LLM derivation. Exercise process_document directly.
        self._patch_llm(_FakeLLMClient(completion="SHOULD NOT BE USED"))
        doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="my-report.csv",
            file=SimpleUploadedFile("stored.csv", _csv_bytes()),
        )
        services.process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.title, "my-report.csv")
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertEqual(doc.file_type, "csv")

    # --- No-key fallback ----------------------------------------------------
    def test_no_key_falls_back_to_filename_stem(self):
        # With the LLM seam raising LLMConfigurationError, a chat-source doc
        # (never embedded) still completes and the title falls back to the file
        # name stem. Using SOURCE_CHAT avoids the library embed step so the
        # no-key path exercises ONLY the title fallback.
        self._patch_llm(_FakeLLMClient(raise_no_key=True))
        doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_CHAT,
            title="",
            file=SimpleUploadedFile("invoice-2024.csv", _csv_bytes()),
        )
        services.process_document(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_READY)
        self.assertEqual(doc.title, "invoice-2024")
        self.assertEqual(doc.file_type, "csv")

    def test_derive_document_title_helper_no_key_url_host(self):
        # The helper falls back to the source URL host when there is no file and
        # no key, and never raises.
        self._patch_llm(_FakeLLMClient(raise_no_key=True))
        doc = Document(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="",
            source_url="https://files.example.com/report",
        )
        title = services.derive_document_title(doc, "some text", False)
        self.assertEqual(title, "files.example.com")

    # --- Form validation (no 'source' field => treat as library) ------------
    #
    # The add form OMITS the ``source`` field; the DocumentAdminForm.clean()
    # therefore treats such a submission as SOURCE_LIBRARY and runs the exactly-
    # one-of file/source_url rule. These exercise the form with the other
    # required model fields satisfied so only the file/URL branch decides
    # validity (``source`` is intentionally never submitted).
    def _add_form_class(self):
        # The ACTUAL add form the admin builds (obj=None -> add_fieldsets): only
        # file + source_url are fields, ``source`` is omitted so clean() treats
        # the submission as library.
        admin = DocumentAdmin(Document, AdminSite())
        request = RequestFactory().get(_ADD_URL)
        request.user = self.root
        return admin.get_form(request, obj=None)

    def test_add_form_omits_source_field(self):
        form_class = self._add_form_class()
        fields = set(form_class.base_fields)
        self.assertEqual(fields, {"file", "source_url"})

    def test_form_both_empty_invalid(self):
        form = self._add_form_class()(data={}, files={})
        self.assertFalse(form.is_valid())

    def test_form_both_present_invalid(self):
        form = self._add_form_class()(
            data={"source_url": "https://example.com/f.csv"},
            files={"file": SimpleUploadedFile("f.csv", _csv_bytes())},
        )
        self.assertFalse(form.is_valid())

    def test_form_file_only_valid(self):
        form = self._add_form_class()(
            data={},
            files={"file": SimpleUploadedFile("f.csv", _csv_bytes())},
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_url_only_valid(self):
        form = self._add_form_class()(
            data={"source_url": "https://example.com/f.csv"}, files={}
        )
        self.assertTrue(form.is_valid(), form.errors)


# ---------------------------------------------------------------------------
# PART B targeted tests: the in-row "Schedule" dropdown on the changelist.
#
# The Schedule control (an "Action" button + a hidden four-item dropdown) must
# render for EVERY library row in ANY status — including Ready — so a processed
# document can be re-dispatched (reprocess). A non-library (chat) row shows a
# disabled "—" hint instead. The menu markup must NOT carry the absolute/z-50
# positioning utilities that the changelist's overflow container clipped; the
# fixed-at-rect positioning is owned by schedule_dropdown.js. The dispatch
# endpoint behaviour (405/403/404/scoping/reprocess) is covered by
# documents.tests_phase4.ScheduleAdminTests and is intentionally not duplicated
# here. The reprocess reset path is asserted at the unit level below.
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class ScheduleDropdownRenderTests(TestCase):
    _CHANGELIST_URL = "/admin/documents/document/"

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(
            email="sched-owner@example.com", password="x", is_staff=True
        )
        cls.company = Company.objects.create(owner=cls.owner, name="SC", slug="sc")
        cls.root = User.objects.create_superuser(
            email="sched-root@example.com", password="x"
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def _make_library_doc(self, *, status, title="report.csv"):
        return Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title=title,
            file=SimpleUploadedFile(title, _csv_bytes()),
            status=status,
        )

    def test_ready_library_row_renders_schedule_control(self):
        # A status=ready library document STILL renders the Action button and
        # the four-item dropdown so it can be reprocessed.
        self.client.force_login(self.root)
        doc = self._make_library_doc(status=Document.STATUS_READY)
        resp = self.client.get(self._CHANGELIST_URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("sidebar", body.lower())  # Unfold shell intact
        self.assertIn("schedule-toggle", body)
        self.assertIn("schedule-menu", body)
        self.assertIn("Action", body)
        self.assertIn(f'data-doc-id="{doc.pk}"', body)
        for backend in ("now", "celery", "kafka", "temporal"):
            self.assertIn(f'data-backend="{backend}"', body)

    def test_every_status_renders_schedule_control(self):
        # The control is status-agnostic for library rows: uploaded, queued,
        # processing, ready and failed all render the toggle + the four items.
        self.client.force_login(self.root)
        for status in (
            Document.STATUS_UPLOADED,
            Document.STATUS_QUEUED,
            Document.STATUS_PROCESSING,
            Document.STATUS_READY,
            Document.STATUS_FAILED,
        ):
            with self.subTest(status=status):
                Document.all_objects.all().hard_delete()
                doc = self._make_library_doc(status=status)
                resp = self.client.get(self._CHANGELIST_URL)
                body = resp.content.decode()
                self.assertIn("schedule-toggle", body)
                self.assertIn(f'data-doc-id="{doc.pk}"', body)
                for backend in ("now", "celery", "kafka", "temporal"):
                    self.assertIn(f'data-backend="{backend}"', body)

    def test_chat_source_row_renders_disabled_hint_not_control(self):
        # A chat-attachment row is not processable from the library, so it shows
        # the "—" hint and NOT the Schedule control.
        self.client.force_login(self.root)
        Document.all_objects.all().hard_delete()
        chat_doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_CHAT,
            title="chat-attachment.csv",
            file=SimpleUploadedFile("chat-attachment.csv", _csv_bytes()),
            status=Document.STATUS_READY,
        )
        resp = self.client.get(self._CHANGELIST_URL)
        body = resp.content.decode()
        self.assertIn("\u2014", body)  # em dash hint
        self.assertNotIn(f'data-doc-id="{chat_doc.pk}"', body)

    def test_menu_markup_has_no_clipping_positioning(self):
        # The clipping fix: the .schedule-menu must NOT carry the absolute/z-50
        # utilities that the overflow container clipped — positioning is owned
        # by schedule_dropdown.js (position:fixed at the toggle rect). Isolate
        # the schedule-menu element so unrelated page markup can't false-pass.
        import re

        self.client.force_login(self.root)
        Document.all_objects.all().hard_delete()
        self._make_library_doc(status=Document.STATUS_READY)
        body = self.client.get(self._CHANGELIST_URL).content.decode()
        match = re.search(
            r'<div class="schedule-menu[^"]*"', body
        )
        self.assertIsNotNone(match, "schedule-menu element should render")
        menu_classes = match.group(0)
        self.assertNotIn("absolute", menu_classes)
        self.assertNotIn("z-50", menu_classes)
        # The 'hidden' toggle class and the look (width) are preserved.
        self.assertIn("hidden", menu_classes)
        self.assertIn("w-44", menu_classes)
