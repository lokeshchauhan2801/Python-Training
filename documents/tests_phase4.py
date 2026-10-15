"""Phase-4 targeted tests: pluggable processing backends.

Each test runs with NO broker/server and NO LLM key. The seams are exercised
directly (dispatcher routing, surface convergence, Kafka handler decode/route,
file-OR-URL validation, SSRF rejection, and the backend-selection admin action)
by patching the transport boundaries so nothing hits a real broker, socket, or
vector store. Library processing that would embed is patched with a fake LLM
client + tmp CHROMA_PATH, mirroring the Phase-2 harness.
"""
from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from company.models import Company

from . import tasks
from .forms import DocumentAdminForm
from .models import Document, DocumentChunk

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix="documents-phase4-")


def _csv_bytes() -> bytes:
    return b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"


def _make_document(company, user, filename="data.csv", data=None, **extra):
    return Document.objects.create(
        company=company,
        uploaded_by=user,
        source=Document.SOURCE_LIBRARY,
        title=filename,
        file=SimpleUploadedFile(filename, data or _csv_bytes()),
        **extra,
    )


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class DispatcherRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    @override_settings(DEBUG=True)
    def test_celery_debug_runs_on_inline_executor(self):
        doc = _make_document(self.company, self.owner)
        with mock.patch.object(tasks._local_executor, "submit") as submit:
            task_id, resolved = tasks.dispatch_document_processing(
                str(doc.pk), backend="celery"
            )
        self.assertEqual(resolved, "local-background")
        submit.assert_called_once()
        # Submitted callable is the task's .run with the document id.
        args = submit.call_args.args
        self.assertEqual(args[0], tasks.process_document_task.run)
        self.assertEqual(args[1], str(doc.pk))
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_QUEUED)
        self.assertEqual(doc.processing_backend, "celery")

    @override_settings(DEBUG=True)
    def test_kafka_uses_dispatcher_not_executor(self):
        doc = _make_document(self.company, self.owner)
        fake_dispatcher = mock.Mock()
        fake_dispatcher.dispatch.return_value = "evt-123"
        with mock.patch(
            "infrastructure.processing.dispatcher.ProcessingDispatcher.from_name",
            return_value=fake_dispatcher,
        ) as from_name, mock.patch.object(
            tasks._local_executor, "submit"
        ) as submit:
            job_id, resolved = tasks.dispatch_document_processing(
                str(doc.pk), backend="kafka"
            )
        self.assertEqual(resolved, "kafka")
        self.assertEqual(job_id, "evt-123")
        from_name.assert_called_once_with("kafka")
        fake_dispatcher.dispatch.assert_called_once()
        submit.assert_not_called()

    @override_settings(DEBUG=True)
    def test_temporal_uses_dispatcher_not_executor(self):
        doc = _make_document(self.company, self.owner)
        fake_dispatcher = mock.Mock()
        fake_dispatcher.dispatch.return_value = "doc-123"
        with mock.patch(
            "infrastructure.processing.dispatcher.ProcessingDispatcher.from_name",
            return_value=fake_dispatcher,
        ) as from_name, mock.patch.object(
            tasks._local_executor, "submit"
        ) as submit:
            job_id, resolved = tasks.dispatch_document_processing(
                str(doc.pk), backend="temporal"
            )
        self.assertEqual(resolved, "temporal")
        self.assertEqual(job_id, "doc-123")
        from_name.assert_called_once_with("temporal")
        submit.assert_not_called()

    def test_unknown_backend_raises(self):
        from infrastructure.processing.dispatcher import ProcessingDispatcher

        with self.assertRaises(ValueError):
            ProcessingDispatcher.from_name("bogus")


# ---------------------------------------------------------------------------
# Surface convergence (handlers + kafka consumer)
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class SurfaceConvergenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_run_document_processing_converges_and_injects_chunk_count(self):
        from infrastructure.processing import handlers

        doc = _make_document(self.company, self.owner)
        Document.objects.filter(pk=doc.pk).update(chunk_count=7)
        with mock.patch.object(
            tasks.process_document_task,
            "run",
            return_value={"status": Document.STATUS_READY, "document_id": str(doc.pk)},
        ) as run:
            result = handlers.run_document_processing(
                surface="documents", document_id=str(doc.pk), backend="kafka"
            )
        run.assert_called_once()
        self.assertEqual(result["chunk_count"], 7)
        self.assertEqual(result["status"], Document.STATUS_READY)

    def test_handle_event_unknown_surface_raises(self):
        from infrastructure.kafka.consumer import handle_event
        from infrastructure.kafka.interfaces import DomainEvent

        event = DomainEvent(
            event_type="document.processing.requested",
            company_id="",
            user_id="",
            resource_id="abc",
            payload={"surface": "nope"},
        )
        with self.assertRaises(ValueError):
            handle_event(event)

    def test_handle_message_malformed_returns_none(self):
        from infrastructure.kafka.consumer import handle_message

        self.assertIsNone(handle_message(b"not-json"))
        self.assertIsNone(handle_message(b'{"missing":"fields"}'))

    def test_handle_event_routes_documents_through_retry(self):
        from infrastructure.kafka.consumer import handle_event
        from infrastructure.kafka.interfaces import DomainEvent

        doc = _make_document(self.company, self.owner)
        event = DomainEvent(
            event_type="document.processing.requested",
            company_id=str(self.company.id),
            user_id=str(self.owner.id),
            resource_id=str(doc.pk),
            payload={"surface": "documents", "backend": "kafka"},
        )
        with mock.patch(
            "infrastructure.processing.handlers.run_document_processing",
            return_value={"status": "ready", "chunk_count": 3},
        ) as run:
            result = handle_event(event)
        run.assert_called_once()
        self.assertEqual(result["chunk_count"], 3)


# ---------------------------------------------------------------------------
# File-OR-URL validation
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class FileOrUrlValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_model_clean_rejects_both_present(self):
        doc = Document(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="x.csv",
            file=SimpleUploadedFile("x.csv", _csv_bytes()),
            source_url="https://example.com/f.csv",
        )
        with self.assertRaises(ValidationError):
            doc.clean()

    def test_model_clean_rejects_both_empty(self):
        doc = Document(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="x.csv",
        )
        with self.assertRaises(ValidationError):
            doc.clean()

    def test_model_clean_accepts_exactly_one(self):
        file_doc = Document(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="x.csv",
            file=SimpleUploadedFile("x.csv", _csv_bytes()),
        )
        file_doc.clean()  # no raise

        url_doc = Document(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="x.csv",
            source_url="https://example.com/f.csv",
        )
        url_doc.clean()  # no raise

    def test_admin_form_surfaces_both_empty_error(self):
        form = DocumentAdminForm(
            data={
                "source": Document.SOURCE_LIBRARY,
                "title": "x.csv",
                "company": str(self.company.id),
                "file_type": "csv",
                "status": Document.STATUS_UPLOADED,
                "processing_backend": "celery",
                "processing_progress": 0,
                "chunk_count": 0,
                "processing_attempts": 0,
                "file_size": 0,
            }
        )
        self.assertFalse(form.is_valid())


# ---------------------------------------------------------------------------
# URL SSRF rejection
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class UrlSsrfRejectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_loopback_url_rejected_no_file_no_network(self):
        import socket as socket_module

        from .url_source import UrlSourceError, download_source_to_document

        doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="remote",
            source_url="http://internal.example/file.csv",
        )

        def _fake_getaddrinfo(host, port, *a, **k):
            # Resolve the host to a loopback address so the SSRF policy blocks it.
            return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        # If any outbound connection were attempted the test would fail loudly.
        with mock.patch("socket.getaddrinfo", _fake_getaddrinfo), mock.patch(
            "urllib.request.OpenerDirector.open",
            side_effect=AssertionError("no outbound request allowed"),
        ):
            with self.assertRaises(UrlSourceError):
                download_source_to_document(doc)
        # No file was saved.
        self.assertFalse(bool(doc.file))

    def test_task_marks_url_download_failure(self):
        from .url_source import UrlSourceError

        doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.owner,
            source=Document.SOURCE_LIBRARY,
            title="remote",
            source_url="http://internal.example/file.csv",
        )
        with mock.patch(
            "documents.url_source.download_source_to_document",
            side_effect=UrlSourceError("URL not allowed"),
        ):
            with self.assertRaises(UrlSourceError):
                tasks.process_document_task.run(str(doc.pk))
        doc.refresh_from_db()
        self.assertEqual(doc.status, Document.STATUS_FAILED)
        self.assertIn("URL not allowed", doc.processing_error)


# ---------------------------------------------------------------------------
# Backend-selection admin action (scoped + CSRF)
# ---------------------------------------------------------------------------
@override_settings(MEDIA_ROOT=_MEDIA)
class ProcessWithAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="owner@example.com", password="x")
        cls.owner.is_staff = True
        cls.owner.save(update_fields=["is_staff"])
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.other_owner = User.objects.create_user(email="other@example.com", password="x")
        cls.other_owner.is_staff = True
        cls.other_owner.save(update_fields=["is_staff"])
        cls.other_co = Company.objects.create(
            owner=cls.other_owner, name="X", slug="x"
        )

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        # is_staff owners can log into admin but need model permissions for the
        # custom row-action URLs. Grant superuser-free access by making them
        # staff with the documents permissions via a superuser login shortcut:
        # the simplest reliable path is to use a superuser for the GET/POST
        # permission checks while still exercising get_queryset scoping through
        # a non-superuser. We log in as a superuser owner of the company.
        self.admin = User.objects.create_superuser(
            email="root@example.com", password="x"
        )

    def _process_with_url(self, doc):
        return reverse("admin:documents_document_process_with", args=[doc.pk])

    def test_get_renders_unfold_shell(self):
        self.client.force_login(self.admin)
        doc = _make_document(self.company, self.owner)
        resp = self.client.get(self._process_with_url(doc))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("sidebar", body.lower())
        self.assertIn("Celery", body)
        self.assertIn("Temporal", body)

    def test_post_sets_backend_and_dispatches(self):
        self.client.force_login(self.admin)
        doc = _make_document(self.company, self.owner)
        with mock.patch(
            "documents.admin.dispatch_document_processing",
            return_value=("job-1", "kafka"),
        ) as dispatch:
            resp = self.client.post(
                self._process_with_url(doc), {"backend": "kafka"}
            )
        self.assertEqual(resp.status_code, 302)
        dispatch.assert_called_once()
        _args, kwargs = dispatch.call_args
        self.assertEqual(kwargs.get("backend"), "kafka")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_backend, "kafka")

    def test_out_of_scope_404_for_non_superuser(self):
        # A non-superuser staff owner cannot act on another company's document.
        other_doc = _make_document(self.other_co, self.other_owner)
        self.client.force_login(self.owner)
        resp = self.client.get(self._process_with_url(other_doc))
        self.assertEqual(resp.status_code, 404)
