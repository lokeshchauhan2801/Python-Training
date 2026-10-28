"""Focused tests for the stateful 'add documents to the knowledge base'
conversation: addable-set computation, pending-state save, deterministic
selection parsing, the view-level seam, the tool-level handler, and the
broadened status-intent detection.

These live in their own module (imported by ``chat.tests`` via Django's test
discovery of the ``chat`` package) so the large ``chat/tests.py`` stays
readable. All dispatch is mocked; no Temporal/network/LLM key is required.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from company.models import Company
from documents.models import Document

from .models import ConversationSession
from .services import kb

User = get_user_model()


def _doc(company, user, title, *, source=None, status=None, size=0, chunks=0):
    return Document.objects.create(
        company=company,
        title=title,
        source=source or Document.SOURCE_CHAT,
        status=status or Document.STATUS_READY,
        uploaded_by=user,
        file_size=size,
        chunk_count=chunks,
    )


class IntentDetectionTests(TestCase):
    """is_status_request / is_add_to_kb_request classification."""

    def test_status_phrasings_true(self):
        for text in [
            "show me the status",
            "show status",
            "processing status",
            "what's processing",
            "what is processing",
            "is my file done",
            "is my document ready",
            "check the status of my upload",
        ]:
            self.assertTrue(kb.is_status_request(text), text)

    def test_ordinary_question_not_status(self):
        self.assertFalse(
            kb.is_status_request("what is the capital of France")
        )
        self.assertFalse(
            kb.is_status_request("what is the status of the French economy")
        )

    def test_add_to_kb_phrasings_true(self):
        self.assertTrue(
            kb.is_add_to_kb_request("add the document to the company documents")
        )
        self.assertTrue(
            kb.is_add_to_kb_request("add documents to the knowledge base")
        )


class AddableDocumentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="add@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="Add Co", slug="add-co"
        )
        cls.session = ConversationSession.objects.create(
            company=cls.company, user=cls.user, title="S"
        )

    def test_excludes_already_embedded_and_dedup_and_order(self):
        # Addable chat files.
        a = _doc(self.company, self.user, "bravo.pdf", size=10)
        b = _doc(self.company, self.user, "alpha.pdf", size=20)
        # Duplicate upload of alpha (same title+size) -> collapsed.
        _doc(self.company, self.user, "alpha.pdf", size=20)
        # Already embedded (library + ready + chunks>0) -> excluded.
        embedded = _doc(
            self.company,
            self.user,
            "charlie.pdf",
            source=Document.SOURCE_LIBRARY,
            chunks=5,
        )
        self.session.documents.add(a, b, embedded)
        # (include the duplicate alpha on the session too)
        for d in Document.objects.filter(company=self.company, title="alpha.pdf"):
            self.session.documents.add(d)

        files = kb.addable_documents(
            conversation=self.session, company=self.company
        )
        titles = [d.title for d in files]
        # alpha deduped to one, bravo kept, charlie excluded; sorted by title.
        self.assertEqual(titles, ["alpha.pdf", "bravo.pdf"])

    def test_already_in_kb_by_title_excluded(self):
        # An existing processed KB document with the same title.
        _doc(
            self.company,
            self.user,
            "report.pdf",
            source=Document.SOURCE_LIBRARY,
            chunks=3,
        )
        chat_dup = _doc(self.company, self.user, "report.pdf")
        self.session.documents.add(chat_dup)
        files = kb.addable_documents(
            conversation=self.session, company=self.company
        )
        self.assertEqual(files, [])


class PendingStateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="pend@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="Pend Co", slug="pend-co"
        )
        cls.session = ConversationSession.objects.create(
            company=cls.company, user=cls.user, title="S"
        )

    def test_multi_file_saves_pending_no_dispatch(self):
        d1 = _doc(self.company, self.user, "aa.pdf")
        d2 = _doc(self.company, self.user, "bb.pdf")
        self.session.documents.add(d1, d2)
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            result = kb.add_session_attachments_to_kb(
                conversation=self.session,
                company=self.company,
                attachment_ids=None,
                actor=self.user,
            )
        mock_dispatch.assert_not_called()
        self.assertTrue(result.get("pending"))
        self.session.refresh_from_db()
        pending = self.session.metadata.get("pending_kb_selection")
        self.assertEqual([f["title"] for f in pending["files"]], ["aa.pdf", "bb.pdf"])

    def test_single_file_dispatches_no_pending(self):
        d1 = _doc(self.company, self.user, "only.pdf")
        self.session.documents.add(d1)
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.return_value = (f"doc-{d1.id}", "temporal")
            result = kb.add_session_attachments_to_kb(
                conversation=self.session,
                company=self.company,
                attachment_ids=None,
                actor=self.user,
            )
        mock_dispatch.assert_called_once()
        self.assertEqual(len(result["queued"]), 1)
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)


class ResolveSelectionTests(TestCase):
    def setUp(self):
        self.files = [
            {"id": "ID1", "title": "annual-report.pdf"},
            {"id": "ID2", "title": "invoice.csv"},
            {"id": "ID3", "title": "contract.docx"},
            {"id": "ID4", "title": "photo.png"},
        ]

    def _ids(self, answer):
        return kb.resolve_selection(answer, self.files)

    def test_all(self):
        r = self._ids("all")
        self.assertEqual(r["action"], "all")
        self.assertEqual(r["ids"], ["ID1", "ID2", "ID3", "ID4"])

    def test_both_everything(self):
        self.assertEqual(self._ids("everything")["ids"], ["ID1", "ID2", "ID3", "ID4"])

    def test_second_one_variants(self):
        for ans in ["second one", "the 2nd", "number 2", "2", "file two"]:
            r = self._ids(ans)
            self.assertEqual(r["ids"], ["ID2"], ans)

    def test_multiple_numbers(self):
        self.assertEqual(self._ids("1 and 3")["ids"], ["ID1", "ID3"])
        self.assertEqual(self._ids("1,3")["ids"], ["ID1", "ID3"])
        self.assertEqual(self._ids("two and four")["ids"], ["ID2", "ID4"])

    def test_first_three_last_two(self):
        self.assertEqual(self._ids("first three")["ids"], ["ID1", "ID2", "ID3"])
        self.assertEqual(self._ids("last two")["ids"], ["ID3", "ID4"])
        self.assertEqual(self._ids("first")["ids"], ["ID1"])
        self.assertEqual(self._ids("last")["ids"], ["ID4"])

    def test_filename_and_substring(self):
        self.assertEqual(self._ids("invoice.csv")["ids"], ["ID2"])
        self.assertEqual(self._ids("CONTRACT")["ids"], ["ID3"])

    def test_cancel(self):
        self.assertEqual(self._ids("never mind")["action"], "cancel")
        self.assertEqual(self._ids("cancel")["action"], "cancel")

    def test_gibberish_none(self):
        r = self._ids("xyzzy qux")
        self.assertEqual(r["action"], "none")
        self.assertEqual(r["ids"], [])

    def test_out_of_range_ignored(self):
        self.assertEqual(self._ids("9")["ids"], [])


class PendingSelectionSendTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="seam@example.com", password="pw")
        cls.company = Company.objects.create(
            owner=cls.user, name="Seam Co", slug="seam-co"
        )

    def setUp(self):
        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user, title="S"
        )
        self.d1 = _doc(self.company, self.user, "aa.pdf")
        self.d2 = _doc(self.company, self.user, "bb.pdf")
        self.session.documents.add(self.d1, self.d2)
        kb.save_pending_selection(
            conversation=self.session,
            files=[self.d1, self.d2],
            actor=self.user,
        )
        self.client.force_login(self.user)

    def _send(self, question):
        return self.client.post(
            reverse("chat:send", args=[self.session.pk]),
            {"question": question},
        )

    def test_all_dispatches_and_clears(self):
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.side_effect = [
                (f"doc-{self.d1.id}", "temporal"),
                (f"doc-{self.d2.id}", "temporal"),
            ]
            resp = self._send("all")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(mock_dispatch.call_count, 2)
        dispatched_ids = {c.args[0] for c in mock_dispatch.call_args_list}
        self.assertEqual(dispatched_ids, {self.d1.id, self.d2.id})
        for c in mock_dispatch.call_args_list:
            self.assertEqual(c.kwargs["backend"], "temporal")
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)
        meta = resp.json()["assistant"]["metadata"]
        self.assertEqual(len(meta["live_status_document_ids"]), 2)
        self.assertIn("temporal_links", meta)

    def test_second_one_dispatches_exactly_one(self):
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.return_value = (f"doc-{self.d2.id}", "temporal")
            resp = self._send("second one")
        self.assertEqual(resp.status_code, 201)
        mock_dispatch.assert_called_once()
        self.assertEqual(mock_dispatch.call_args.args[0], self.d2.id)
        self.assertEqual(mock_dispatch.call_args.kwargs["backend"], "temporal")
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)

    def test_cancel_clears_no_dispatch(self):
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            resp = self._send("never mind")
        self.assertEqual(resp.status_code, 201)
        mock_dispatch.assert_not_called()
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)

    def test_gibberish_keeps_state_and_reasks(self):
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            resp = self._send("asdf qwerty")
        self.assertEqual(resp.status_code, 201)
        mock_dispatch.assert_not_called()
        self.session.refresh_from_db()
        # State is KEPT so the next message can still resolve it (no silent loop).
        self.assertIn("pending_kb_selection", self.session.metadata)
        self.assertIn("didn't catch", resp.json()["assistant"]["content"])

    def test_seam_fires_before_router_with_active_graph(self):
        # Seed an active entry Agent so _orchestrator_for would pick the
        # LangGraph router; the seam must still intercept the follow-up.
        from agents.models import Agent, Tool

        tool = Tool.objects.create(
            company=self.company,
            slug="general_assistant",
            name="General assistant",
            type="general_assistant",
        )
        agent = Agent.objects.create(
            company=self.company,
            slug="chat_agent",
            connected_from_start=True,
            connected_to_end=True,
            is_active=True,
        )
        agent.tools.add(tool)
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.return_value = (f"doc-{self.d1.id}", "temporal")
            resp = self._send("first")
        self.assertEqual(resp.status_code, 201)
        mock_dispatch.assert_called_once()
        self.assertEqual(mock_dispatch.call_args.args[0], self.d1.id)
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)


class _FakeIndexLLM:
    """Fake LLM whose generate_structured returns preset 1-based indices."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, indices):
        self._indices = indices

    def generate_structured(self, prompt, schema):
        return schema(indices=self._indices)


class KBToolSelectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="tool@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="Tool Co", slug="tool-co"
        )

    def setUp(self):
        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user, title="S"
        )
        self.d1 = _doc(self.company, self.user, "aa.pdf")
        self.d2 = _doc(self.company, self.user, "bb.pdf")
        self.d3 = _doc(self.company, self.user, "cc.pdf")
        self.session.documents.add(self.d1, self.d2, self.d3)
        kb.save_pending_selection(
            conversation=self.session,
            files=[self.d1, self.d2, self.d3],
            actor=self.user,
        )

    def _ctx(self, message, llm=None):
        from chat.services.llm_tools import ToolContext

        return ToolContext(
            user=self.user,
            company=self.company,
            conversation=self.session,
            message_text=message,
            llm=llm,
        )

    def test_deterministic_second_one(self):
        from chat.services.llm_tools import _add_to_knowledge_base

        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.return_value = (f"doc-{self.d2.id}", "temporal")
            result = _add_to_knowledge_base(self._ctx("second one"))
        mock_dispatch.assert_called_once()
        self.assertEqual(mock_dispatch.call_args.args[0], self.d2.id)
        self.assertEqual(result.tool_used, "add_to_knowledge_base")
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)

    def test_llm_assist_when_deterministic_abstains(self):
        from chat.services.llm_tools import _add_to_knowledge_base

        llm = _FakeIndexLLM(indices=[1, 3])
        with mock.patch(
            "documents.tasks.dispatch_document_processing"
        ) as mock_dispatch:
            mock_dispatch.side_effect = [
                (f"doc-{self.d1.id}", "temporal"),
                (f"doc-{self.d3.id}", "temporal"),
            ]
            # A free-text answer the deterministic parser cannot resolve
            # (no numbers/ordinals/all/first/last/filename tokens).
            result = _add_to_knowledge_base(
                self._ctx("whichever ones seem important to you", llm=llm)
            )
        self.assertEqual(mock_dispatch.call_count, 2)
        dispatched = {c.args[0] for c in mock_dispatch.call_args_list}
        self.assertEqual(dispatched, {self.d1.id, self.d3.id})
        self.assertEqual(result.tool_used, "add_to_knowledge_base")
        self.session.refresh_from_db()
        self.assertNotIn("pending_kb_selection", self.session.metadata)
