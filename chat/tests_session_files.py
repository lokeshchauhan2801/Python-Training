"""Tests for the chat session file store (``chat.services.session_files``)."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company
from documents.controls import STOPPED_BY_USER_MESSAGE
from documents.models import Document, DocumentStage

from .admin import ConversationSessionAdmin
from .api.serializers import ConversationSessionSerializer
from .file_test_support import attach_entry, make_upload
from .models import ConversationSession
from .services import session_files
from .services.session_files import (
    FAILED_HINT,
    FileUnavailable,
    UploadRejected,
    derive_state,
    file_row,
    files_by_ids,
    list_files,
    locked_files,
)

User = get_user_model()


class _MediaMixin:
    """Each test class writes into its own temporary MEDIA_ROOT."""

    def _use_temp_media(self):
        media = tempfile.mkdtemp(prefix="chat-session-files-")
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, media, True)
        return media


class _Fixture(_MediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="sf@example.com", password="pw")
        cls.other = User.objects.create_user(email="sf-other@example.com", password="pw")
        cls.company = Company.objects.create(owner=cls.user, name="SF Co", slug="sf-co")
        cls.other_company = Company.objects.create(
            owner=cls.other, name="SF Other", slug="sf-other"
        )

    def setUp(self):
        self.media = self._use_temp_media()
        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user, title="Files"
        )

    def _fresh(self, session=None):
        return ConversationSession.objects.get(pk=(session or self.session).pk)

    def _document(self, status=Document.STATUS_READY, content=b"a,b\n1,2\n", **extra):
        return Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            source=Document.SOURCE_LIBRARY,
            status=status,
            title="data.csv",
            file=SimpleUploadedFile("data.csv", content),
            file_type="csv",
            **extra,
        )

    def _link(self, entry, document):
        with locked_files(self.session) as state:
            state.set_document(entry["id"], document.pk)

    def _set_entry(self, entry_id, **changes):
        session = self._fresh()
        context = session.context
        for name in (session_files.UNPROCESSED_KEY, session_files.PROCESSED_KEY):
            for entry in context.get(name, []):
                if entry["id"] == entry_id:
                    entry.update(changes)
        ConversationSession.objects.filter(pk=session.pk).update(context=context)
        self.session = self._fresh()


class AttachTests(_Fixture):
    """S1 and S8: attach validation, storage location, the 200-file cap."""

    def _attach(self, upload):
        self.client.force_login(self.user)
        data = {"file": upload} if upload is not None else {}
        return self.client.post(reverse("chat:attach", args=[self.session.pk]), data)

    def test_attach_creates_entry_and_no_document(self):
        before = Document.all_objects.count()
        resp = self._attach(SimpleUploadedFile("flow_country.csv", b"a,b\n1,2\n"))
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(set(data), {"id", "title", "file_type"})
        self.assertEqual(data["file_type"], "csv")
        self.assertEqual(Document.all_objects.count(), before)

        (entry,) = self._fresh().context["unprocessed_docs"]
        self.assertEqual(entry["id"], data["id"])
        self.assertEqual(entry["size"], 8)
        self.assertIsNone(entry["document_id"])
        prefix = f"unsettled/{self.session.pk}/{data['id']}/"
        self.assertTrue(entry["storage_key"].startswith(prefix))
        self.assertTrue(default_storage.exists(entry["storage_key"]))
        self.assertNotIn("unsettled", resp.content.decode())

    def test_attach_validation_errors(self):
        cases = [
            (None, 400, "A file is required."),
            (SimpleUploadedFile("empty.txt", b""), 400, "The file is empty."),
            (
                SimpleUploadedFile("tool.exe", b"MZ"),
                400,
                session_files.UNSUPPORTED_MESSAGE,
            ),
        ]
        for upload, status, message in cases:
            resp = self._attach(upload)
            self.assertEqual(resp.status_code, status)
            self.assertEqual(resp.json(), {"error": message})
        with override_settings(CHAT_ATTACHMENT_MAX_BYTES=10):
            resp = self._attach(SimpleUploadedFile("big.txt", b"x" * 11))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json(), {"error": "File is too large (max 25 MB)."})
        self.assertEqual(self._fresh().context.get("unprocessed_docs", []), [])
        self.assertFalse(os.path.exists(os.path.join(self.media, "unsettled")))

    def test_traversal_name_stays_in_entry_directory(self):
        upload = make_upload("x.csv", b"a,b\n")
        upload._name = "../../etc/passwd.csv"
        entry = session_files.store_upload(self.session, upload, self.user)
        self.assertEqual(entry["title"], "passwd.csv")
        self.assertEqual(
            entry["storage_key"],
            f"unsettled/{self.session.pk}/{entry['id']}/passwd.csv",
        )
        path = os.path.realpath(default_storage.path(entry["storage_key"]))
        self.assertTrue(path.startswith(os.path.realpath(self.media) + os.sep))

    def test_title_strips_control_characters(self):
        upload = make_upload("x.txt", b"hi")
        upload._name = "re\x00port\x1f.txt"
        entry = session_files.store_upload(self.session, upload, self.user)
        self.assertEqual(entry["title"], "report.txt")

    def test_long_dotted_name_fits_document_path(self):
        """F7: a 150-char name with many dots attaches, and its copy fits 100."""
        name = ("a.b." * 37) + "xy.csv"
        self.assertGreaterEqual(len(name), 150)
        entry = attach_entry(self.session, name, b"a,b\n1,2\n", self.user)
        stored = entry["storage_key"].rsplit("/", 1)[-1]
        self.assertTrue(stored.endswith(".csv"))
        self.assertNotIn(".", stored[: -len(".csv")])
        self.assertLessEqual(len(stored), 64)
        target = Document._meta.get_field("file").generate_filename(None, stored)
        self.assertLess(len(target), 100)
        saved = default_storage.save(target, ContentFile(b"x"), max_length=100)
        self.assertLess(len(saved), 100)

    def test_cap_rejects_201st_file_and_deletes_its_bytes(self):
        """S8: at 200 entries the next attach is refused and leaves no bytes."""
        filler = [
            {"id": f"{i:026d}", "title": f"f{i}.txt", "attached_at": "2020-01-01T00:00:00+00:00"}
            for i in range(200)
        ]
        ConversationSession.objects.filter(pk=self.session.pk).update(
            context={"unprocessed_docs": filler[:150], "processed_docs": filler[150:]}
        )
        resp = self._attach(SimpleUploadedFile("one.txt", b"hello"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "This chat already has 200 files."})
        session_dir = os.path.join(self.media, "unsettled", str(self.session.pk))
        stored = [f for _, _, files in os.walk(session_dir) for f in files]
        self.assertEqual(stored, [])
        self.assertEqual(self._fresh().context["unprocessed_docs"], filler[:150])

    def test_unexpected_error_returns_500_and_deletes_bytes(self):
        with mock.patch.object(
            session_files, "locked_files", side_effect=RuntimeError("boom")
        ), self.assertLogs("chat.views", "ERROR"):
            resp = self._attach(SimpleUploadedFile("one.txt", b"hello"))
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json(), {"error": "Could not attach the file."})
        session_dir = os.path.join(self.media, "unsettled", str(self.session.pk))
        stored = [f for _, _, files in os.walk(session_dir) for f in files]
        self.assertEqual(stored, [])

    def test_upload_rejected_carries_status(self):
        with self.assertRaises(UploadRejected) as caught:
            session_files.store_upload(self.session, None, self.user)
        self.assertEqual(caught.exception.status, 400)


class ReaderTests(_Fixture):
    """S3: the SessionFile reader before and after processing."""

    def test_unprocessed_csv_opens_from_its_key(self):
        entry = attach_entry(self.session, "a.csv", b"x,y\n1,2\n", self.user)
        (f,) = files_by_ids(self.session, [entry["id"]])
        with f.open() as handle:
            self.assertEqual(handle.read(), b"x,y\n1,2\n")
        self.assertTrue(f.is_csv)
        self.assertTrue(f.readable)

    def test_processed_entry_opens_document_file(self):
        entry = attach_entry(self.session, "a.csv", b"old", self.user)
        document = self._document(content=b"from-document")
        self._link(entry, document)
        (f,) = files_by_ids(self._fresh(), [entry["id"]])
        with f.open() as handle:
            self.assertEqual(handle.read(), b"from-document")
        self.assertEqual(f.as_extractable().pk, document.pk)

    def test_tampered_keys_raise_file_unavailable(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        other = ConversationSession.objects.create(company=self.company, user=self.user)
        default_storage.save("documents/secret.csv", ContentFile(b"secret"))
        for key in (
            "documents/secret.csv",
            f"unsettled/{self.session.pk}/../../documents/secret.csv",
            f"unsettled/{other.pk}/{entry['id']}/a.csv",
            "/etc/passwd",
        ):
            self._set_entry(entry["id"], storage_key=key)
            (f,) = files_by_ids(self.session, [entry["id"]])
            with self.assertLogs("chat.services.session_files", "WARNING"):
                with self.assertRaises(FileUnavailable):
                    f.open()
            with self.assertLogs("chat.services.session_files", "WARNING"):
                with self.assertRaises(FileUnavailable):
                    f.as_extractable()

    def test_missing_bytes_raise_file_unavailable(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        default_storage.delete(entry["storage_key"])
        (f,) = files_by_ids(self.session, [entry["id"]])
        with self.assertRaises(FileUnavailable):
            f.open()

    def test_as_extractable_creates_no_document(self):
        entry = attach_entry(self.session, "n.txt", b"The text.", self.user)
        before = Document.all_objects.count()
        (f,) = files_by_ids(self.session, [entry["id"]])
        from documents.services import extract_document_text

        self.assertEqual(extract_document_text(f.as_extractable()), "The text.")
        self.assertEqual(Document.all_objects.count(), before)

    def test_files_by_ids_rejects_unknown_and_too_many(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        other = ConversationSession.objects.create(company=self.company, user=self.user)
        foreign = attach_entry(other, "b.csv", b"y", self.user)
        for ids in (["short"], [foreign["id"]], [entry["id"], "Z" * 26]):
            with self.assertRaises(session_files.UnknownEntry):
                files_by_ids(self.session, ids)
        with self.assertRaises(session_files.TooManyIds):
            files_by_ids(self.session, [f"{i:026d}" for i in range(51)])
        self.assertEqual(
            [f.id for f in files_by_ids(self.session, [entry["id"], entry["id"]])],
            [entry["id"]],
        )

    def test_list_files_orders_by_attach_and_scopes_documents(self):
        first = attach_entry(self.session, "1.csv", b"x", self.user)
        second = attach_entry(self.session, "2.csv", b"y", self.user)
        foreign_doc = Document.objects.create(
            company=self.other_company,
            uploaded_by=self.other,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            title="theirs.csv",
            file_type="csv",
        )
        self._link(first, foreign_doc)
        with locked_files(self.session) as state:
            state.move_to_processed(first["id"])
        files = list_files(self._fresh())
        self.assertEqual([f.id for f in files], [first["id"], second["id"]])
        self.assertIsNone(files[0].document)
        self.assertEqual(files[0].state, "removed")
        self.assertFalse(files[0].readable)


class ReconcileTests(_Fixture):
    """S4: lazy reconcile moves entries to match their Documents."""

    def test_ready_moves_to_processed_once(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        self._link(entry, self._document(status=Document.STATUS_READY))
        session = self._fresh()
        session_files.reconcile(session)
        context = self._fresh().context
        self.assertEqual(context["unprocessed_docs"], [])
        (moved,) = context["processed_docs"]
        self.assertEqual(moved["id"], entry["id"])
        self.assertTrue(moved["processed_at"])

        session = self._fresh()
        with CaptureQueriesContext(connection) as queries:
            session_files.reconcile(session)
        self.assertFalse(
            [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith("UPDATE")]
        )

    def test_not_ready_moves_back_and_removed_stays(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        document = self._document(status=Document.STATUS_READY)
        self._link(entry, document)
        session_files.reconcile(self._fresh())
        Document.objects.filter(pk=document.pk).update(status=Document.STATUS_PROCESSING)
        session_files.reconcile(self._fresh())
        context = self._fresh().context
        (back,) = context["unprocessed_docs"]
        self.assertNotIn("processed_at", back)
        self.assertEqual(context["processed_docs"], [])

        Document.objects.filter(pk=document.pk).update(status=Document.STATUS_READY)
        session_files.reconcile(self._fresh())
        document.hard_delete()
        files = session_files.reconcile(self._fresh())
        self.assertEqual([f.state for f in files], ["removed"])
        self.assertEqual(len(self._fresh().context["processed_docs"]), 1)

    def test_has_eligible(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        self.assertFalse(session_files.has_eligible(self.session, "stop"))
        document = self._document(status=Document.STATUS_QUEUED)
        self._link(entry, document)
        self.assertTrue(session_files.has_eligible(self._fresh(), "stop"))
        self.assertFalse(session_files.has_eligible(self._fresh(), "resume"))
        Document.objects.filter(pk=document.pk).update(
            status=Document.STATUS_FAILED, processing_error=STOPPED_BY_USER_MESSAGE
        )
        self.assertTrue(session_files.has_eligible(self._fresh(), "resume"))


class StateTests(_Fixture):
    """S5 and F10: derive_state for every G.3 row, labels and the failed hint."""

    def test_every_state_row(self):
        entry = {"id": "x" * 26, "document_id": None}
        self.assertEqual(derive_state(entry, None), "not_started")
        linked = {"id": "x" * 26, "document_id": "y" * 26}
        self.assertEqual(derive_state(linked, None), "removed")
        rows = [
            (Document.STATUS_UPLOADED, "", "not_started"),
            (Document.STATUS_QUEUED, "", "queued"),
            (Document.STATUS_PROCESSING, "", "processing"),
            (Document.STATUS_FAILED, STOPPED_BY_USER_MESSAGE, "stopped"),
            (Document.STATUS_FAILED, "Embedding service down.", "failed"),
            (Document.STATUS_READY, "", "processed"),
        ]
        for status, error, expected in rows:
            document = Document(status=status, processing_error=error)
            self.assertEqual(derive_state(linked, document), expected, status)
        deleted = Document(status=Document.STATUS_READY, is_deleted=True)
        self.assertEqual(derive_state(linked, deleted), "removed")

    def _row_for(self, **doc_fields):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        document = self._document(**doc_fields)
        self._link(entry, document)
        (f,) = files_by_ids(self._fresh(), [entry["id"]])
        return file_row(f), document

    def test_failed_row_has_hint_and_no_failed_at_failed(self):
        row, _ = self._row_for(
            status=Document.STATUS_FAILED,
            processing_stage="failed",
            processing_error="Embedding service down. " * 20,
        )
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["label"], "Failed")
        self.assertEqual(row["stage"], "")
        self.assertEqual(len(row["error"]), 200)
        self.assertEqual(row["hint"], FAILED_HINT)
        self.assertEqual(
            row["hint"],
            "Say “try again” or “restart” to continue from the last completed step.",
        )
        self.assertEqual(row["actions"], ["resume"])
        self.assertTrue(row["selectable"])
        self.assertTrue(row["terminal"])

    def test_failed_row_names_its_stage(self):
        row, document = self._row_for(
            status=Document.STATUS_FAILED, processing_error="bad file"
        )
        DocumentStage.objects.create(
            company=self.company, document=document, name="extract_text",
            order=1, status=DocumentStage.STATUS_FAILED,
        )
        (f,) = files_by_ids(self._fresh(), [row["id"]])
        row = file_row(f)
        self.assertEqual(row["label"], "Failed at extract text")
        self.assertEqual(row["error"], "bad file")

    def test_processing_and_other_rows(self):
        row, _ = self._row_for(
            status=Document.STATUS_PROCESSING,
            processing_stage="embed_and_store_chunks",
            processing_progress=40,
        )
        self.assertEqual(row["label"], "Processing · embed and store chunks 40%")
        self.assertEqual(row["actions"], ["stop"])
        self.assertFalse(row["selectable"])
        self.assertFalse(row["terminal"])
        self.assertEqual(row["hint"], "")
        self.assertNotIn("storage_key", row)

        entry = attach_entry(self.session, "b.csv", b"x", self.user)
        (f,) = files_by_ids(self._fresh(), [entry["id"]])
        row = file_row(f)
        self.assertEqual((row["state"], row["label"]), ("not_started", "Not processed"))
        self.assertIsNone(row["document_id"])


class LockTests(_Fixture):
    """F8b: locked_files is not re-entrant."""

    def test_nested_block_raises_and_writes_nothing(self):
        with self.assertRaisesMessage(RuntimeError, "locked_files is not re-entrant"):
            with locked_files(self.session) as outer:
                outer.append({"id": "a" * 26, "title": "a.txt"})
                with locked_files(self.session):
                    pass
        self.assertNotIn("unprocessed_docs", self._fresh().context)
        with locked_files(self.session) as state:
            state.append({"id": "b" * 26, "title": "b.txt"})
        self.assertEqual(len(self._fresh().context["unprocessed_docs"]), 1)

    def test_other_context_keys_are_preserved(self):
        ConversationSession.objects.filter(pk=self.session.pk).update(
            context={"other": {"keep": True}}
        )
        attach_entry(self.session, "a.txt", b"x", self.user)
        context = self._fresh().context
        self.assertEqual(context["other"], {"keep": True})
        self.assertEqual(context["processed_docs"], [])


class ApiAndStaleSaveTests(_Fixture):
    """S6 and S7: API projection and no stale full saves."""

    def _detail_url(self):
        return f"/api/v1/chat/conversation-session/{self.session.pk}/"

    def test_api_hides_storage_keys_without_document_query(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        self._link(entry, self._document())
        client = APIClient()
        client.force_authenticate(self.user)
        with CaptureQueriesContext(connection) as queries:
            resp = client.get(self._detail_url())
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn("unsettled/", body)
        self.assertNotIn("storage_key", body)
        self.assertNotIn("documents_document", " ".join(q["sql"] for q in queries))
        (public,) = resp.json()["context"]["unprocessed_docs"]
        self.assertEqual(set(public), set(session_files.PUBLIC_FIELDS))

    def test_patch_of_context_is_ignored(self):
        entry = attach_entry(self.session, "a.csv", b"x", self.user)
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.patch(
            self._detail_url(), {"title": "Renamed", "context": {}}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        session = self._fresh()
        self.assertEqual(session.title, "Renamed")
        self.assertEqual(session.context["unprocessed_docs"][0]["id"], entry["id"])

    def test_serializer_save_on_stale_instance_keeps_entries(self):
        stale = self._fresh()
        entry = attach_entry(self._fresh(), "a.csv", b"x", self.user)
        request = RequestFactory().patch("/")
        request.user = self.user
        serializer = ConversationSessionSerializer(
            stale, data={"title": "x"}, partial=True, context={"request": request}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        session = self._fresh()
        self.assertEqual(session.title, "x")
        self.assertEqual(session.context["unprocessed_docs"][0]["id"], entry["id"])

    def test_admin_save_model_on_stale_instance_keeps_entries(self):
        stale = self._fresh()
        entry = attach_entry(self._fresh(), "a.csv", b"x", self.user)
        model_admin = ConversationSessionAdmin(ConversationSession, django_admin.site)
        request = RequestFactory().post("/")
        request.user = self.user
        request.session = {}
        stale.title = "Edited in admin"
        form = mock.Mock(changed_data=["title"])
        model_admin.save_model(request, stale, form, change=True)
        session = self._fresh()
        self.assertEqual(session.title, "Edited in admin")
        self.assertEqual(session.updated_by_id, self.user.pk)
        self.assertEqual(session.context["unprocessed_docs"][0]["id"], entry["id"])


class PurgeTests(_Fixture):
    """F13: every hard delete removes never-processed bytes after commit."""

    def _admin_request(self):
        request = RequestFactory().post("/")
        request.user = self.user
        request.session = {}
        return request

    def _two_files(self, session):
        unsettled = attach_entry(session, "u.csv", b"x", self.user)
        processed = attach_entry(session, "p.csv", b"y", self.user)
        with locked_files(session) as state:
            state.set_document(processed["id"], self._document().pk)
        return unsettled["storage_key"], processed["storage_key"]

    def test_admin_delete_model_purges_on_commit(self):
        unsettled_key, processed_key = self._two_files(self.session)
        model_admin = ConversationSessionAdmin(ConversationSession, django_admin.site)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            model_admin.delete_model(self._admin_request(), self._fresh())
            self.assertTrue(default_storage.exists(unsettled_key))
        for callback in callbacks:
            callback()
        self.assertFalse(default_storage.exists(unsettled_key))
        self.assertTrue(default_storage.exists(processed_key))
        self.assertFalse(ConversationSession.all_objects.filter(pk=self.session.pk).exists())

    def test_admin_delete_queryset_purges_on_commit(self):
        other = ConversationSession.objects.create(company=self.company, user=self.user)
        first = attach_entry(self.session, "a.csv", b"x", self.user)["storage_key"]
        second = attach_entry(other, "b.csv", b"y", self.user)["storage_key"]
        model_admin = ConversationSessionAdmin(ConversationSession, django_admin.site)
        queryset = ConversationSession.all_objects.filter(pk__in=[self.session.pk, other.pk])
        with self.captureOnCommitCallbacks(execute=True):
            model_admin.delete_queryset(self._admin_request(), queryset)
        self.assertFalse(default_storage.exists(first))
        self.assertFalse(default_storage.exists(second))

    def test_delete_view_purges_entry_added_after_load(self):
        stale = self._fresh()
        early = attach_entry(self._fresh(), "early.csv", b"x", self.user)["storage_key"]
        late = attach_entry(self._fresh(), "late.csv", b"y", self.user)["storage_key"]
        self.assertNotIn("unprocessed_docs", stale.context)
        self.client.force_login(self.user)
        with mock.patch("chat.views._resolve_session", return_value=stale):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(reverse("chat:delete_session", args=[stale.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(default_storage.exists(early))
        self.assertFalse(default_storage.exists(late))

    def test_soft_delete_keeps_files(self):
        key = attach_entry(self.session, "a.csv", b"x", self.user)["storage_key"]
        self.client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                reverse("chat:delete_session", args=[self.session.pk]), {"soft": "1"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(default_storage.exists(key))

    def test_purge_logs_os_errors(self):
        with mock.patch.object(default_storage, "delete", side_effect=OSError("busy")):
            with self.assertLogs("chat.services.session_files", "WARNING"):
                session_files.purge_unsettled(["unsettled/x/y/z.csv"])


class ParallelAttachTests(_MediaMixin, TransactionTestCase):
    """S2: five concurrent attaches to one session all keep their entry."""

    def setUp(self):
        self._use_temp_media()
        self.user = User.objects.create_user(email="sf-par@example.com", password="pw")
        company = Company.objects.create(owner=self.user, name="Par Co", slug="par-co")
        self.session = ConversationSession.objects.create(company=company, user=self.user)

    def test_five_parallel_attaches_keep_every_entry(self):
        barrier = threading.Barrier(5)
        errors = []

        def worker(index):
            try:
                session = ConversationSession.objects.get(pk=self.session.pk)
                barrier.wait(timeout=30)
                attach_entry(session, f"f{index}.txt", f"file {index}", self.user)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        entries = ConversationSession.objects.get(pk=self.session.pk).context[
            "unprocessed_docs"
        ]
        self.assertEqual(len({e["id"] for e in entries}), 5)
        self.assertEqual(
            sorted(e["title"] for e in entries), [f"f{i}.txt" for i in range(5)]
        )
