"""File commands in the chat: process / stop / resume / status / list.

Covers design I C2–C13, C10b, C11, C16 (command cases) and C18, plus the
review-3 F1, F3, F5–F10 cases. Temporal is never contacted: the enqueue is the
shared ``fake_enqueue`` and the Temporal backend is a mock.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from company.models import Company
from documents import controls, tasks
from documents.models import Document

from .file_test_support import attach_entry, fake_enqueue
from .models import ConversationSession
from .services import ChatService, file_commands, session_files
from .services.file_commands import PICKER_NOTE

User = get_user_model()

_ENQUEUE = "documents.controls.enqueue_document_processing"
_BACKEND = "documents.controls.TemporalProcessingBackend"
_CSV = "country,people\nUK,3\nFR,2\n"


class _FakeLLM:
    """Records every LLM call; answers with canned text."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, response="llm answer"):
        self._response = response
        self.structured_calls = 0
        self.complete_calls = 0

    def generate_structured(self, prompt, schema):
        self.structured_calls += 1
        return schema(response=self._response, chart=None, title=None)

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.complete_calls += 1
        return self._response

    def embed_query(self, text):
        return [0.0] * 8

    @property
    def calls(self):
        return self.structured_calls + self.complete_calls


class LangGraphEntryAgentMixin:
    """An active Agent with one entry Step (``connected_from_start=True``).

    With it, ``views._orchestrator_for`` returns the LangGraph service. The
    router is stubbed with ``route_to(slug)`` so no router LLM is called.
    """

    langgraph_tools = (
        "add_to_knowledge_base",
        "document_processing_status",
        "general_assistant",
        "document_search",
    )

    def enable_langgraph(self, company=None):
        from agents.models import Agent, Step, Tool

        company = company or self.company
        agent = Agent.objects.create(
            company=company,
            slug="chat_agent",
            is_active=True,
        )
        step = Step.objects.create(
            company=company,
            agent=agent,
            slug="main",
            connected_from_start=True,
            connected_to_end=True,
        )
        for slug in self.langgraph_tools:
            tool = Tool.objects.create(
                company=company, slug=slug, name=slug.replace("_", " "), type=slug
            )
            step.tools.add(tool)
        patcher = mock.patch("basics.langfuse.get_langfuse_client", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        return agent

    def route_to(self, slug):
        return mock.patch(
            "chat.services.langgraph_service.LangGraphChatService._route",
            return_value=(slug, "stubbed route", None),
        )


class _Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="fc@example.com", password="pw")
        cls.other = User.objects.create_user(email="fc-other@example.com", password="pw")
        cls.company = Company.objects.create(owner=cls.user, name="FC Co", slug="fc-co")
        cls.other_company = Company.objects.create(
            owner=cls.other, name="FC Other", slug="fc-other"
        )

    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="chat-file-commands-")
        override = override_settings(MEDIA_ROOT=self.media, TEMPORAL_UI_URL="http://temporal.test")
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)

        self.enqueue = self._patch(_ENQUEUE, side_effect=fake_enqueue)
        self.backend = self._patch(_BACKEND)
        self._patch("chat.services.document_services.find_relevant_chunks", return_value=[])
        self.llm = _FakeLLM()
        self._patch("chat.views._chat_service", side_effect=lambda: ChatService(llm_client=self.llm))

        self.session = self.new_session()
        self.client.force_login(self.user)

    def _patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def new_session(self, user=None, company=None):
        return ConversationSession.objects.create(
            company=company or self.company, user=user or self.user, title="Files"
        )

    def fresh(self, session=None):
        return ConversationSession.objects.get(pk=(session or self.session).pk)

    def attach(self, name, content=_CSV, session=None):
        session = session or self.session
        entry = attach_entry(session, name, content, session.user)
        return entry

    def send(self, question="", *, attachments=(), selected=(), session=None):
        session = session or self.session
        data = {"question": question}
        if attachments:
            data["attachment_ids"] = [e["id"] for e in attachments]
        if selected:
            data["selected_file_ids"] = [e["id"] for e in selected]
        return self.client.post(reverse("chat:send", args=[session.pk]), data)

    def assistant(self, resp):
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["assistant"]

    def entry(self, entry, session=None):
        found = session_files.FilesState.from_context(self.fresh(session).context).find(
            entry["id"]
        )
        return found[1] if found else None

    def doc(self, entry, session=None):
        document_id = self.entry(entry, session)["document_id"]
        return Document.all_objects.get(pk=document_id) if document_id else None

    def set_doc(self, entry, **fields):
        Document.objects.filter(pk=self.doc(entry).pk).update(**fields)

    def process(self, *entries, session=None):
        """Start processing ``entries`` (QUEUED via fake_enqueue)."""
        session = session or self.session
        with self.captureOnCommitCallbacks(execute=True):
            file_commands.process_entries(
                self.fresh(session), [e["id"] for e in entries], self.user
            )

    def in_flight(self, *entries):
        self.process(*entries)
        for e in entries:
            self.set_doc(e, status=Document.STATUS_PROCESSING, processing_stage="extract_text")

    def stopped(self, *entries):
        for e in entries:
            if self.doc(e) is None:
                self.process(e)
            self.set_doc(
                e,
                status=Document.STATUS_FAILED,
                processing_stage="failed",
                processing_error=controls.STOPPED_BY_USER_MESSAGE,
            )

    def failed(self, entry, error="Text extraction crashed"):
        if self.doc(entry) is None:
            self.process(entry)
        self.set_doc(
            entry,
            status=Document.STATUS_FAILED,
            processing_stage="failed",
            processing_error=error,
        )

    def rows(self, session=None):
        resp = self.client.get(reverse("chat:files_json", args=[(session or self.session).pk]))
        self.assertEqual(resp.status_code, 200)
        return {row["title"]: row for row in resp.json()["files"]}

    def company_docs(self):
        return Document.all_objects.filter(company=self.company)


# --------------------------------------------------------------------------
# PROCESS (C2–C8, F6, F7, C10 phase errors, the old KBServiceTests)
# --------------------------------------------------------------------------


class ProcessCommandTests(_Base):
    def test_no_files_reply(self):
        msg = self.assistant(self.send("process this file to company kb"))
        self.assertEqual(msg["content"], file_commands.NO_FILES_REPLY)
        self.assertNotIn("file_panel", msg["metadata"])
        self.assertEqual(msg["model_name"], "knowledge_base")

    def test_two_earlier_files_give_the_picker(self):
        self.attach("county_uk.csv")
        self.attach("education.csv")
        resp = self.send("process this file to company kb")
        msg = self.assistant(resp)
        self.assertEqual(msg["content"], PICKER_NOTE)
        self.assertEqual(
            msg["metadata"]["file_panel"],
            {"mode": "picker", "entry_ids": None, "note": PICKER_NOTE},
        )
        self.assertFalse(resp.json()["selection_consumed"])
        self.assertEqual(self.company_docs().count(), 0)
        self.enqueue.assert_not_called()
        self.assertEqual(self.llm.calls, 0)

    def test_same_turn_attachments(self):
        files = [self.attach(n) for n in ("a.csv", "b.csv", "c.csv")]
        msg = self.assistant(self.send("process this file to company kb", attachments=files))
        self.assertEqual(msg["metadata"]["file_panel"]["mode"], "picker")
        self.assertEqual(msg["metadata"]["file_panel"]["entry_ids"], [f["id"] for f in files])
        self.assertEqual(self.company_docs().count(), 0)

        msg = self.assistant(self.send("process these files", attachments=files))
        self.assertEqual(self.company_docs().count(), 3)
        self.assertEqual(msg["metadata"]["file_panel"]["mode"], "tracker")

    def test_one_attachment_is_processed_directly(self):
        self.attach("older.csv")
        new = self.attach("new.csv")
        msg = self.assistant(self.send("process this file", attachments=[new]))
        self.assertEqual(msg["content"], "Queued “new.csv” for processing.")
        self.assertEqual(self.company_docs().count(), 1)
        self.assertIsNotNone(self.doc(new))

    def test_polite_plural_gives_the_plain_picker(self):
        self.attach("a.csv")
        self.attach("b.csv")
        for text in ("can you process these files?", "process both files"):
            with self.subTest(text=text):
                msg = self.assistant(self.send(text))
                self.assertEqual(msg["metadata"]["file_panel"]["note"], PICKER_NOTE)
                self.assertIsNone(msg["metadata"]["file_panel"]["entry_ids"])
        self.assertEqual(self.company_docs().count(), 0)

    def test_selection_with_empty_question_creates_the_document(self):
        entry = self.attach("flow_country.csv")
        key = entry["storage_key"]
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = self.send(selected=[entry])
        body = resp.json()
        msg = self.assistant(resp)
        self.assertTrue(body["selection_consumed"])
        self.assertEqual(body["user"]["content"], "Process: flow_country.csv")

        document = self.doc(entry)
        self.assertEqual(document.company, self.company)
        self.assertEqual(document.uploaded_by, self.user)
        self.assertEqual(document.session_id, self.session.pk)
        self.assertEqual(document.source, Document.SOURCE_LIBRARY)
        self.assertEqual(document.title, "flow_country.csv")
        self.assertEqual(document.file_type, "csv")
        self.assertEqual(document.file_size, len(_CSV))
        self.assertTrue(document.file.name.startswith("documents/"))
        self.assertTrue(default_storage.exists(document.file.name))
        self.assertEqual(document.status, Document.STATUS_QUEUED)
        self.enqueue.assert_called_once_with(str(document.pk), backend="temporal")

        self.assertTrue(default_storage.exists(key))  # deleted only on commit
        for callback in callbacks:
            callback()
        self.assertFalse(default_storage.exists(key))

        meta = msg["metadata"]
        self.assertEqual(
            meta["file_panel"], {"mode": "tracker", "entry_ids": [entry["id"]], "note": ""}
        )
        self.assertEqual(
            meta["temporal_links"],
            [{
                "title": "flow_country.csv",
                "url": f"http://temporal.test/namespaces/default/workflows/doc-{document.pk}",
                "workflow_id": f"doc-{document.pk}",
            }],
        )
        self.assertNotIn("live_status_document_ids", meta)
        self.assertNotIn("retry_document_ids", meta)

    def test_two_entries_in_one_request_delete_both_unsettled_copies(self):
        a, b = self.attach("a.csv"), self.attach("b.csv")
        with self.captureOnCommitCallbacks(execute=True):
            self.assistant(self.send(selected=[a, b]))
        self.assertFalse(default_storage.exists(a["storage_key"]))
        self.assertFalse(default_storage.exists(b["storage_key"]))
        self.assertEqual(self.company_docs().count(), 2)

    def test_selection_with_a_question_routes_normally(self):
        entry = self.attach("a.csv")
        resp = self.send("what is in this file?", selected=[entry])
        msg = self.assistant(resp)
        self.assertFalse(resp.json()["selection_consumed"])
        self.assertEqual(msg["content"], "llm answer")
        self.enqueue.assert_not_called()

    def test_process_flow_csv(self):
        flow = self.attach("flow_country.csv")
        self.attach("education.csv")
        self.assistant(self.send("process flow csv"))
        self.assertIsNotNone(self.doc(flow))
        self.assertEqual(self.company_docs().count(), 1)

        self.session = self.new_session()
        one = self.attach("flow_country.csv")
        two = self.attach("flow_sales.csv")
        msg = self.assistant(self.send("process flow csv"))
        panel = msg["metadata"]["file_panel"]
        self.assertEqual(panel["mode"], "picker")
        self.assertEqual(panel["entry_ids"], [one["id"], two["id"]])
        self.assertIn("I found several files matching “flow”", msg["content"])
        self.assertEqual(self.company_docs().count(), 1)

        resp = self.send("process these", selected=[one, two])
        self.assistant(resp)
        self.assertTrue(resp.json()["selection_consumed"])
        self.assertEqual(self.company_docs().count(), 3)

    def test_two_named_segments_are_both_processed(self):
        flow = self.attach("flow_country.csv")
        edu = self.attach("education.csv")
        other = self.attach("other.csv")
        msg = self.assistant(self.send("process flow country csv and education csv"))
        self.assertIsNotNone(self.doc(flow))
        self.assertIsNotNone(self.doc(edu))
        self.assertIsNone(self.doc(other))
        self.assertIn("“flow_country.csv”", msg["content"])
        self.assertIn("“education.csv”", msg["content"])

    def test_named_but_missing_gives_the_full_picker(self):
        self.attach("flow_country.csv")
        msg = self.assistant(self.send("process budget csv"))
        self.assertEqual(
            msg["metadata"]["file_panel"]["note"],
            "I couldn't find a file matching “budget”. Here is the list of files in this chat.",
        )
        self.assertIsNone(msg["metadata"]["file_panel"]["entry_ids"])
        self.assertEqual(self.company_docs().count(), 0)

    def test_selection_plus_attachment_processes_only_the_selection(self):
        a = self.attach("a.csv")
        b = self.attach("b.csv")
        resp = self.send(selected=[a], attachments=[b])
        self.assistant(resp)
        self.assertIsNotNone(self.doc(a))
        self.assertIsNone(self.doc(b))
        self.assertEqual(resp.json()["user"]["attachments"], [{"id": b["id"], "title": "b.csv"}])
        self.assertEqual(self.rows()["b.csv"]["state"], "not_started")

    def test_reselecting_a_processed_entry_reprocesses_the_same_row(self):
        entry = self.attach("a.csv")
        self.process(entry)
        document = self.doc(entry)
        self.set_doc(entry, status=Document.STATUS_READY)
        self.assertEqual(self.rows()["a.csv"]["state"], "processed")
        self.assertIn(entry["id"], [e["id"] for e in self.fresh().context["processed_docs"]])

        with mock.patch(
            "chat.services.file_commands.controls.reprocess_document",
            wraps=controls.reprocess_document,
        ) as reprocess:
            self.assistant(self.send(selected=[entry]))
        reprocess.assert_called_once()
        self.assertEqual(self.doc(entry).pk, document.pk)
        self.assertEqual(self.company_docs().count(), 1)
        self.assertEqual(self.doc(entry).status, Document.STATUS_QUEUED)
        self.assertIn(entry["id"], [e["id"] for e in self.fresh().context["unprocessed_docs"]])

    def test_bytes_gone_after_another_request_started_it_is_already_processing(self):
        entry = self.attach("a.csv")
        real = file_commands._precopy
        raced = []

        def racing(session, ids):
            # Another request processes the entry and its commit deletes the
            # unsettled bytes after this request read the entry without a lock.
            if not raced:
                raced.append(True)
                with self.captureOnCommitCallbacks(execute=True):
                    file_commands.process_entries(self.fresh(), ids, self.user)
            return real(session, ids)

        with mock.patch("chat.services.file_commands._precopy", side_effect=racing):
            msg = self.assistant(self.send(selected=[entry]))
        self.assertEqual(msg["content"], "“a.csv” is already processing.")
        self.assertEqual(self.company_docs().count(), 1)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_missing_bytes_report_missing(self):
        entry = self.attach("a.csv")
        default_storage.delete(entry["storage_key"])
        msg = self.assistant(self.send(selected=[entry]))
        self.assertEqual(msg["content"], "“a.csv”: the file is missing; please attach it again.")
        self.assertEqual(self.company_docs().count(), 0)

    def _documents_dir_files(self):
        root = os.path.join(self.media, "documents")
        return [f for _d, _s, names in os.walk(root) for f in names]

    def test_phase_one_error_changes_nothing(self):
        entry = self.attach("a.csv")
        with mock.patch(
            "chat.services.file_commands.controls.start_document",
            side_effect=RuntimeError("db down"),
        ), self.assertLogs("chat.services.file_commands", "ERROR"):
            msg = self.assistant(self.send(selected=[entry]))
        self.assertEqual(msg["content"], file_commands.START_FAILED)
        self.assertEqual(self.company_docs().count(), 0)
        self.assertIsNone(self.entry(entry)["document_id"])
        self.assertTrue(default_storage.exists(entry["storage_key"]))
        self.assertEqual(self._documents_dir_files(), [])

    def test_phase_zero_error_changes_nothing(self):
        entry = self.attach("a.csv")
        with mock.patch.object(
            session_files.SessionFile, "open", side_effect=OSError("disk")
        ), self.assertLogs("chat.services.file_commands", "ERROR"):
            msg = self.assistant(self.send(selected=[entry]))
        self.assertEqual(msg["content"], file_commands.START_FAILED)
        self.assertEqual(self.company_docs().count(), 0)
        self.assertTrue(default_storage.exists(entry["storage_key"]))

    def test_list_shows_the_picker_of_all_files(self):
        self.attach("a.csv")
        msg = self.assistant(self.send("show all files"))
        self.assertEqual(
            msg["metadata"]["file_panel"],
            {"mode": "picker", "entry_ids": None, "note": PICKER_NOTE},
        )


# --------------------------------------------------------------------------
# STATUS (C10, C10b, F3, F10)
# --------------------------------------------------------------------------


class StatusCommandTests(_Base):
    def test_status_without_session_files_keeps_live_rows(self):
        queued = Document.objects.create(
            company=self.company, uploaded_by=self.user, title="lib.pdf",
            source=Document.SOURCE_LIBRARY, status=Document.STATUS_QUEUED,
        )
        msg = self.assistant(self.send("status"))
        self.assertEqual(msg["metadata"]["live_status_document_ids"], [str(queued.pk)])
        self.assertIn("lib.pdf", msg["content"])
        self.assertNotIn("file_panel", msg["metadata"])

    def test_status_with_a_document_ulid_gets_the_single_report(self):
        self.attach("a.csv")
        other = Document.objects.create(
            company=self.company, uploaded_by=self.user, title="lib.pdf",
            source=Document.SOURCE_LIBRARY, status=Document.STATUS_READY,
        )
        msg = self.assistant(self.send(f"status of {other.pk}"))
        self.assertIn("Here is the status for that document:", msg["content"])
        self.assertIn("lib.pdf", msg["content"])

    def test_status_lines_for_every_file(self):
        a, b, c = self.attach("a.csv"), self.attach("b.csv"), self.attach("c.csv")
        self.in_flight(b)
        self.stopped(c)
        msg = self.assistant(self.send("what's the status"))
        self.assertIn("- a.csv: not started", msg["content"])
        self.assertIn("- b.csv: processing (", msg["content"])
        self.assertIn("- c.csv: stopped — say “resume c.csv” to continue", msg["content"])
        self.assertEqual(msg["metadata"]["file_panel"], {"mode": "tracker", "entry_ids": None, "note": ""})

    def test_is_it_done_reports_status_without_the_llm(self):
        entry = self.attach("a.csv")
        self.in_flight(entry)
        msg = self.assistant(self.send("is it done?"))
        self.assertIn("- a.csv: processing", msg["content"])
        self.assertEqual(self.llm.calls, 0)

    def test_is_it_done_with_nothing_started_reaches_the_llm(self):
        self.attach("a.csv")
        msg = self.assistant(self.send("is it done?"))
        self.assertEqual(msg["content"], "llm answer")
        self.assertEqual(self.llm.calls, 1)

    def test_dispatch_failure_shows_failed_with_the_hint(self):
        entry = self.attach("a.csv")
        self.process(entry)
        document = self.doc(entry)
        with mock.patch("documents.tasks.close_old_connections"), mock.patch(
            "documents.tasks.dispatch_document_processing",
            side_effect=RuntimeError("temporal unreachable"),
        ):
            tasks._dispatch_queued_document(str(document.pk), "temporal")
        row = self.rows()["a.csv"]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["actions"], ["resume"])
        self.assertEqual(row["hint"], session_files.FAILED_HINT)
        self.assertNotIn("at failed", row["label"])
        msg = self.assistant(self.send("status"))
        self.assertIn(session_files.FAILED_HINT, msg["content"])
        self.assertIn("temporal unreachable", msg["content"])
        self.assertNotIn("at failed", msg["content"])

    def test_resume_with_a_selection_uses_only_the_checked_files(self):
        a, b = self.attach("a.csv"), self.attach("b.csv")
        self.failed(a)
        self.failed(b)
        self.enqueue.reset_mock()
        resp = self.send("try again", selected=[a])
        self.assistant(resp)
        self.assertTrue(resp.json()["selection_consumed"])
        self.enqueue.assert_called_once_with(str(self.doc(a).pk), backend="temporal")
        self.assertEqual(self.doc(b).status, Document.STATUS_FAILED)

    def test_stop_ignores_the_selection(self):
        a, b = self.attach("a.csv"), self.attach("b.csv")
        self.in_flight(a, b)
        resp = self.send("stop", selected=[a])
        self.assistant(resp)
        self.assertFalse(resp.json()["selection_consumed"])
        self.assertTrue(controls.is_stopped(self.doc(a)))
        self.assertTrue(controls.is_stopped(self.doc(b)))


# --------------------------------------------------------------------------
# STOP / RESUME (C12, C13, F1, F5) and the admin parity
# --------------------------------------------------------------------------


class StopResumeTests(_Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = User.objects.create_superuser(email="fc-admin@example.com", password="pw")
        cls.admin_company = Company.objects.create(
            owner=cls.admin, name="FC Admin", slug="fc-admin"
        )

    def twin(self, document):
        """A library row in the same state, for the admin views."""
        return Document.objects.create(
            company=self.admin_company, uploaded_by=self.admin, title="twin.csv",
            source=Document.SOURCE_LIBRARY, status=document.status,
            processing_backend=document.processing_backend,
            processing_stage=document.processing_stage,
            processing_progress=document.processing_progress,
            processing_error=document.processing_error,
            file_type="csv",
        )

    def fields(self, document):
        document.refresh_from_db()
        return (document.status, document.processing_stage, document.processing_error)

    def admin_post(self, name, document):
        client = Client()
        client.force_login(self.admin)
        with mock.patch("documents.admin.TemporalProcessingBackend"), mock.patch(
            "documents.admin.enqueue_document_processing", side_effect=fake_enqueue
        ):
            return client.post(reverse(f"admin:documents_document_{name}", args=[document.pk]))

    def test_text_stop_and_resume_match_the_admin_views(self):
        flow = self.attach("flow_country.csv")
        self.in_flight(flow)
        twin = self.twin(self.doc(flow))
        self.admin_post("temporal_cancel", twin)
        msg = self.assistant(self.send("stop flow csv"))
        self.assertEqual(msg["content"], "Stopped “flow_country.csv”.")
        self.backend.return_value.terminate.assert_called_once_with(f"doc-{self.doc(flow).pk}")
        self.assertEqual(self.fields(self.doc(flow)), self.fields(twin))
        self.assertEqual(twin.processing_error, controls.STOPPED_BY_USER_MESSAGE)
        self.assertEqual(self.rows()["flow_country.csv"]["state"], "stopped")

        self.admin_post("temporal_retry", twin)
        self.enqueue.reset_mock()
        msg = self.assistant(self.send("try again"))
        self.assertEqual(msg["content"], "Resuming “flow_country.csv” from the last completed step.")
        self.enqueue.assert_called_once_with(str(self.doc(flow).pk), backend="temporal")
        self.assertEqual(self.fields(self.doc(flow)), self.fields(twin))

    def test_endpoint_stop_and_resume_match_the_admin_views(self):
        entry = self.attach("a.csv")
        self.in_flight(entry)
        twin = self.twin(self.doc(entry))
        self.admin_post("temporal_cancel", twin)
        url = lambda action: reverse("chat:file_action", args=[self.session.pk, entry["id"], action])  # noqa: E731
        resp = self.client.post(url("stop"))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["file"]["state"], "stopped")
        self.assertEqual(resp.json()["file"]["label"].split(" ")[0], "Stopped")
        self.assertEqual(self.fields(self.doc(entry)), self.fields(twin))

        self.admin_post("temporal_retry", twin)
        resp = self.client.post(url("resume"))
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["file"]["state"], "queued")
        self.assertEqual(self.fields(self.doc(entry)), self.fields(twin))

        self.failed(entry)
        self.assertEqual(self.rows()["a.csv"]["state"], "failed")
        resp = self.client.post(url("retry"))
        self.assertEqual(resp.status_code, 202, resp.content)

    def test_unknown_or_ambiguous_names_stop_nothing(self):
        one, two = self.attach("flow_country.csv"), self.attach("flow_sales.csv")
        self.in_flight(one, two)
        msg = self.assistant(self.send("stop budget csv"))
        self.assertEqual(msg["content"], "I couldn't find a file matching “budget” in this chat.")
        self.assertEqual(msg["metadata"]["file_panel"], {"mode": "tracker", "entry_ids": None, "note": ""})
        msg = self.assistant(self.send("stop flow csv"))
        self.assertIn("Use the Stop button on the one you mean", msg["content"])
        self.assertEqual(msg["metadata"]["file_panel"]["entry_ids"], [one["id"], two["id"]])
        self.backend.return_value.terminate.assert_not_called()
        self.assertEqual(self.doc(one).status, Document.STATUS_PROCESSING)

    def test_every_stop_phrase_stops_every_in_flight_file(self):
        a, b = self.attach("alpha.csv"), self.attach("beta.csv")
        self.in_flight(a, b)
        for text in (
            "stop", "stop processing", "stop everything", "can you stop it?",
            "ok stop", "stop the last file", "can you stop processing?",
            "please stop the processing, I don't need it",
        ):
            with self.subTest(text=text):
                for e in (a, b):
                    self.set_doc(e, status=Document.STATUS_PROCESSING, processing_error="")
                self.assistant(self.send(text))
                self.assertTrue(controls.is_stopped(self.doc(a)), text)
                self.assertTrue(controls.is_stopped(self.doc(b)), text)

    def test_resume_phrases_resume_every_stopped_or_failed_file(self):
        a, b = self.attach("alpha.csv"), self.attach("beta.csv")
        for text in ("resume all stopped files", "could you try again on the failed files"):
            with self.subTest(text=text):
                self.stopped(a)
                self.failed(b)
                self.assistant(self.send(text))
                self.assertEqual(self.doc(a).status, Document.STATUS_QUEUED)
                self.assertEqual(self.doc(b).status, Document.STATUS_QUEUED)

    def test_status_of_all_files_has_no_not_found_line(self):
        self.attach("alpha.csv")
        self.attach("beta.csv")
        msg = self.assistant(self.send("status of all files in this chat"))
        self.assertNotIn("couldn't find", msg["content"])
        self.assertIn("- alpha.csv: not started", msg["content"])
        self.assertIn("- beta.csv: not started", msg["content"])

    def test_short_name_commands_act_on_the_named_file(self):
        flow = self.attach("flow_country.csv")
        self.attach("education.csv")
        self.in_flight(flow)
        self.assistant(self.send("stop flow"))
        self.assertTrue(controls.is_stopped(self.doc(flow)))
        self.assistant(self.send("resume flow"))
        self.assertEqual(self.doc(flow).status, Document.STATUS_QUEUED)
        self.failed(flow)
        self.assistant(self.send("retry flow"))
        self.assertEqual(self.doc(flow).status, Document.STATUS_QUEUED)

    def test_ordinary_uses_of_the_words_reach_the_llm(self):
        entry = self.attach("flow_country.csv")
        self.stopped(entry)
        for text in ("stop using jargon", "continue explaining the document"):
            with self.subTest(text=text):
                msg = self.assistant(self.send(text))
                self.assertEqual(msg["content"], "llm answer")
        self.assertTrue(controls.is_stopped(self.doc(entry)))

    def test_bare_continue_with_nothing_stopped_reaches_the_llm(self):
        self.attach("a.csv")
        msg = self.assistant(self.send("continue"))
        self.assertEqual(msg["content"], "llm answer")


# --------------------------------------------------------------------------
# Endpoints (C9, C11, C18, F8a, F9)
# --------------------------------------------------------------------------


class EndpointTests(_Base):
    def action(self, entry, action, session=None):
        session = session or self.session
        return self.client.post(
            reverse("chat:file_action", args=[session.pk, entry["id"], action])
        )

    def test_files_json(self):
        self.attach("a.csv")
        resp = self.client.get(reverse("chat:files_json", args=[self.session.pk]))
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["all_terminal"])
        self.assertEqual(body["files"][0]["state"], "not_started")
        self.assertNotIn("storage_key", resp.content.decode())
        self.assertNotIn("unsettled/", resp.content.decode())
        self.assertNotIn(self.media, resp.content.decode())

    def test_not_started_and_removed_get_409(self):
        entry = self.attach("a.csv")
        for action in ("stop", "resume"):
            resp = self.action(entry, action)
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(
                resp.json()["error"],
                "“a.csv” hasn't been processed yet. Select it and press Process.",
            )
        self.process(entry)
        self.doc(entry).delete()  # soft delete -> removed
        resp = self.action(entry, "stop")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "“a.csv” was removed from Documents.")
        self.assertEqual(resp.json()["file"]["state"], "removed")

    def test_disallowed_state_gets_the_controls_message(self):
        entry = self.attach("a.csv")
        self.process(entry)  # queued
        resp = self.action(entry, "resume")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"], "Only a failed document can be retried.")
        self.assertFalse(resp.json()["ok"])

    def test_resume_twice_second_gets_409(self):
        entry = self.attach("a.csv")
        self.failed(entry)
        self.enqueue.reset_mock()
        self.assertEqual(self.action(entry, "resume").status_code, 202)
        self.assertEqual(self.action(entry, "resume").status_code, 409)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_process_entries_twice_enqueues_once(self):
        entry = self.attach("a.csv")
        self.process(entry)
        self.process(entry)
        self.assertEqual(self.enqueue.call_count, 1)
        failed = self.attach("b.csv")
        self.failed(failed)
        self.enqueue.reset_mock()
        self.process(failed)
        self.process(failed)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_unknown_and_foreign_ids(self):
        mine = self.attach("a.csv")
        other_session = self.new_session()
        theirs = self.attach("b.csv", session=other_session)
        foreign_company = self.new_session(user=self.other, company=self.other_company)
        foreign = self.attach("c.csv", session=foreign_company)

        self.assertEqual(self.action(mine, "explode").status_code, 404)
        self.assertEqual(self.action({"id": "X" * 26}, "stop").status_code, 404)
        self.assertEqual(self.action({"id": "short"}, "stop").status_code, 404)
        with mock.patch.object(session_files.SessionFile, "open") as opened:
            for entry in (theirs, foreign):
                self.assertEqual(self.action(entry, "stop").status_code, 404)
                resp = self.send("hi", attachments=[entry])
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"], "Unknown attachment.")
                resp = self.send("", selected=[entry])
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"], "Some selected files are not in this chat.")
                resp = self.send("", selected=[mine, entry])
                self.assertEqual(resp.status_code, 400)
        opened.assert_not_called()
        self.assertEqual(self.company_docs().count(), 0)
        self.enqueue.assert_not_called()

    def test_hygiene(self):
        entry = self.attach("a.csv")
        file_url = reverse("chat:file_action", args=[self.session.pk, entry["id"], "stop"])
        self.assertEqual(self.client.get(file_url).status_code, 405)
        self.assertEqual(self.client.get(reverse("chat:send", args=[self.session.pk])).status_code, 405)
        self.assertEqual(
            self.client.post(reverse("chat:files_json", args=[self.session.pk])).status_code, 405
        )

        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(file_url).status_code, 403)
        self.assertEqual(
            strict.post(reverse("chat:send", args=[self.session.pk]), {"question": "status"}).status_code,
            403,
        )

        anonymous = Client()
        for url in (file_url, reverse("chat:files_json", args=[self.session.pk])):
            resp = anonymous.post(url) if "files/" in url else anonymous.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("login", resp["Location"])

        self.process(entry)
        for resp in (
            self.client.get(reverse("chat:files_json", args=[self.session.pk])),
            self.send(selected=[entry]),
            self.send("status"),
        ):
            self.assertNotIn(self.media, resp.content.decode())
            self.assertNotIn("storage_key", resp.content.decode())

    def test_empty_send_is_rejected(self):
        resp = self.send("")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "Type a message or attach a file.")


class LegacyReprocessTests(_Base):
    """Review-3 F9: the old chat Retry route maps every status."""

    def legacy(self, status, source=Document.SOURCE_LIBRARY):
        return Document.objects.create(
            company=self.company, uploaded_by=self.user, title="legacy.pdf",
            source=source, status=status, processing_backend="temporal",
            file=SimpleUploadedFile("legacy.pdf", b"%PDF-1.4"), file_type="pdf",
        )

    def post(self, document):
        return self.client.post(
            reverse("chat:reprocess_document", args=[self.session.pk]),
            {"document_id": str(document.pk)},
        )

    def test_status_mapping(self):
        ready = self.legacy(Document.STATUS_READY)
        resp = self.post(ready)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("already in the knowledge base", resp.json()["message"])

        processing = self.legacy(Document.STATUS_PROCESSING)
        resp = self.post(processing)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already processing", resp.json()["message"])

        uploaded = self.legacy(Document.STATUS_UPLOADED)
        self.assertEqual(self.post(uploaded).status_code, 200)
        self.enqueue.assert_called_once_with(str(uploaded.pk), backend="temporal")

        chat_row = self.legacy(Document.STATUS_UPLOADED, source=Document.SOURCE_CHAT)
        resp = self.post(chat_row)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["message"], "Only library documents can be processed.")

    def test_failed_row_double_click_enqueues_once(self):
        failed = self.legacy(Document.STATUS_FAILED)
        first = self.post(failed)
        second = self.post(failed)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["workflow_id"], f"doc-{failed.pk}")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_foreign_document_is_409_not_found(self):
        foreign = Document.objects.create(
            company=self.other_company, uploaded_by=self.other, title="x.pdf",
            source=Document.SOURCE_LIBRARY, status=Document.STATUS_FAILED,
        )
        resp = self.post(foreign)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["message"], "That document is not available.")
        self.enqueue.assert_not_called()


# --------------------------------------------------------------------------
# Both orchestration paths (C16)
# --------------------------------------------------------------------------


class BothPathsTests(LangGraphEntryAgentMixin, _Base):
    """The plain ChatService path and the LangGraph tool path give the same reply."""

    def titled_panel(self, msg, session):
        panel = msg["metadata"].get("file_panel")
        if not panel or panel["entry_ids"] is None:
            return panel
        titles = {f.id: f.title for f in session_files.list_files(self.fresh(session))}
        return {**panel, "entry_ids": [titles[i] for i in panel["entry_ids"]]}

    def run_both(self, setup, text, tool, *, attach_turn=False):
        """``setup(session) -> attachments``; returns (plain, langgraph) replies."""
        replies = []
        for path in ("plain", "langgraph"):
            session = self.new_session()
            attachments = setup(session) or []
            if path == "langgraph":
                if not hasattr(self, "_agent"):
                    self._agent = self.enable_langgraph()
                with mock.patch("chat.views._file_command_reply", return_value=None), self.route_to(tool):
                    resp = self.send(text, attachments=attachments if attach_turn else (), session=session)
                msg = self.assistant(resp)
                self.assertEqual(msg["metadata"]["source"], "langgraph")
                self.assertEqual(msg["metadata"]["tool_used"], tool)
            else:
                msg = self.assistant(
                    self.send(text, attachments=attachments if attach_turn else (), session=session)
                )
                self.assertEqual(msg["model_name"], "knowledge_base")
            replies.append((msg["content"], self.titled_panel(msg, session), session))
        (plain_content, plain_panel, _), (lg_content, lg_panel, _) = replies
        self.assertEqual(plain_content, lg_content)
        self.assertEqual(plain_panel, lg_panel)
        self.assertEqual(self.llm.calls, 0)
        return replies

    def test_picker(self):
        def setup(session):
            self.attach("county_uk.csv", session=session)
            self.attach("education.csv", session=session)

        replies = self.run_both(setup, "process this file to company kb", "add_to_knowledge_base")
        self.assertEqual(replies[0][1]["mode"], "picker")
        self.assertEqual(self.company_docs().count(), 0)

    def test_process_one_attachment(self):
        def setup(session):
            return [self.attach("a.csv", session=session)]

        replies = self.run_both(setup, "process this file", "add_to_knowledge_base", attach_turn=True)
        self.assertEqual(replies[0][0], "Queued “a.csv” for processing.")
        self.assertEqual(replies[0][1], {"mode": "tracker", "entry_ids": ["a.csv"], "note": ""})
        self.assertEqual(self.company_docs().count(), 2)

    def test_process_ambiguous_flow(self):
        def setup(session):
            self.attach("flow_country.csv", session=session)
            self.attach("flow_sales.csv", session=session)

        replies = self.run_both(setup, "process flow csv", "add_to_knowledge_base")
        self.assertEqual(replies[0][1]["entry_ids"], ["flow_country.csv", "flow_sales.csv"])

    def test_stop_named_file(self):
        def setup(session):
            entry = self.attach("flow_country.csv", session=session)
            self.process(entry, session=session)
            Document.objects.filter(session=session).update(status=Document.STATUS_PROCESSING)

        replies = self.run_both(setup, "stop flow csv", "document_processing_status")
        self.assertEqual(replies[0][0], "Stopped “flow_country.csv”.")
        for _content, _panel, session in replies:
            self.assertTrue(controls.is_stopped(Document.objects.get(session=session)))

    def test_stop_unknown_name(self):
        def setup(session):
            entry = self.attach("flow_country.csv", session=session)
            self.process(entry, session=session)

        replies = self.run_both(setup, "stop budget csv", "add_to_knowledge_base")
        self.assertIn("couldn't find a file matching “budget”", replies[0][0])
        self.backend.return_value.terminate.assert_not_called()

    def test_status(self):
        def setup(session):
            self.attach("a.csv", session=session)

        replies = self.run_both(setup, "status", "document_processing_status")
        self.assertIn("- a.csv: not started", replies[0][0])

    def test_fall_through_reaches_the_orchestrator_on_both_paths(self):
        for text in ("continue", "continue explaining the document"):
            with self.subTest(text=text):
                session = self.new_session()
                self.attach("a.csv", session=session)
                msg = self.assistant(self.send(text, session=session))
                self.assertEqual(msg["content"], "llm answer")
        if not hasattr(self, "_agent"):
            self._agent = self.enable_langgraph()
        for text in ("continue", "continue explaining the document"):
            with self.subTest(text=text, path="langgraph"):
                session = self.new_session()
                self.attach("a.csv", session=session)
                with self.route_to("general_assistant") as route:
                    msg = self.assistant(self.send(text, session=session))
                route.assert_called_once()
                self.assertEqual(msg["metadata"]["source"], "langgraph")
                self.assertNotIn("file_panel", msg["metadata"])
        self.enqueue.assert_not_called()
