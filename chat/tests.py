"""Targeted tests for the chat domain (FEAT-003 + FEAT-004).

Covers the company-match invariant on Conversation.save() and ChatService using
an injected fake LLM client (no network, no key), plus the admin AJAX
``detail-json`` endpoint (authenticated JSON, anonymous blocked) and the DRF
owner/company scoping (a user cannot attach or ``ask`` on another owner's
company/session).
"""

import shutil
import tempfile
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from basics.llm import LLMConfigurationError
from company.models import Company
from documents import services as document_services
from documents.models import Document, DocumentChunk

from .admin import ConversationSessionAdmin
from .models import Conversation, ConversationSession, Message, MessageVersion
from .services import ChatCompletion, ChartData, ChartPoint, ChatService

User = get_user_model()

# Per-module temp MEDIA_ROOT so attach/CSV tests write real files off the repo.
_MEDIA = tempfile.mkdtemp(prefix="chat-tests-media-")


class _FakeLLM:
    provider = "fake"
    model = "fake-model"

    def __init__(self, reply="fake answer"):
        self.reply = reply
        self.calls = []

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.calls.append(messages)
        return self.reply


class _UnconfiguredLLM:
    """A fake client that behaves like a key-less real client."""

    provider = "fake"
    model = "fake-model"

    def complete(self, messages, *, temperature=None, max_tokens=None):
        raise LLMConfigurationError("No LLM API key configured.")


class _FakeStructuredLLM:
    """Fake LLM exposing the RAG seam: generate_structured + embed_query.

    ``generate_structured`` returns a canned ``ChatCompletion`` (so no key /
    network is needed) and records every prompt it is handed so tests can assert
    on the file-vs-no-file prompt branch. ``embed_query`` returns a cheap
    deterministic vector so retrieval never touches a live embeddings backend.
    """

    provider = "fake"
    model = "fake-model"

    def __init__(self, response="fake answer", chart=None):
        self._response = response
        self._chart = chart
        self.prompts = []
        self.structured_calls = 0

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        self.structured_calls += 1
        return schema(response=self._response, chart=self._chart)

    def complete(self, messages, *, temperature=None, max_tokens=None):
        return self._response

    def embed_query(self, text):
        return [0.0] * 8


class ChatInvariantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="a@example.com", password="x")
        cls.owner = User.objects.create_user(email="owner@example.com", password="x")
        cls.company_a = Company.objects.create(owner=cls.owner, name="A", slug="a")
        cls.company_b = Company.objects.create(owner=cls.owner, name="B", slug="b")
        cls.session = ConversationSession.objects.create(
            company=cls.company_a, user=cls.user, title="S1"
        )

    def test_conversation_save_rejects_company_mismatch(self):
        turn = Conversation(
            session=self.session,
            user=self.user,
            company=self.company_b,  # mismatches session.company (A)
            question="q",
        )
        with self.assertRaises(ValidationError):
            turn.save()

    def test_conversation_save_accepts_matching_company(self):
        turn = Conversation(
            session=self.session,
            user=self.user,
            company=self.company_a,
            question="q",
        )
        turn.save()
        self.assertIsNotNone(turn.pk)

    def test_session_id_is_26_char_ulid(self):
        self.assertEqual(len(self.session.session_id), 26)


class ChatServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="u@example.com", password="x")
        cls.owner = User.objects.create_user(email="o@example.com", password="x")
        cls.company = Company.objects.create(owner=cls.owner, name="C", slug="c")
        cls.session = ConversationSession.objects.create(
            company=cls.company, user=cls.user, title="S"
        )

    def test_ask_persists_turn_with_company_and_metadata(self):
        fake = _FakeLLM(reply="hello")
        service = ChatService(llm_client=fake)
        turn = service.ask(
            session=self.session, user=self.user, question="hi"
        )
        self.assertEqual(turn.response, "hello")
        self.assertEqual(turn.company_id, self.company.id)
        self.assertEqual(turn.metadata["provider"], "fake")
        self.assertEqual(turn.metadata["model"], "fake-model")
        self.assertEqual(turn.created_by_id, self.user.id)

    def test_summarize_sets_session_summary(self):
        fake = _FakeLLM(reply="a summary")
        ChatService(llm_client=fake).ask(
            session=self.session, user=self.user, question="hi"
        )
        summary = ChatService(llm_client=fake).summarize(
            session=self.session, actor=self.user
        )
        self.session.refresh_from_db()
        self.assertEqual(summary, "a summary")
        self.assertEqual(self.session.summary, "a summary")
        self.assertEqual(self.session.model_name, "fake-model")


class AdminDetailJsonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_superuser(
            email="staff@example.com", password="pw"
        )
        cls.company = Company.objects.create(
            owner=cls.staff, name="C", slug="c"
        )
        cls.session = ConversationSession.objects.create(
            company=cls.company, user=cls.staff, title="Admin session"
        )

    def _url(self):
        return reverse(
            "admin:chat_conversationsession_detail_json", args=[self.session.pk]
        )

    def test_authenticated_admin_gets_json(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        data = resp.json()
        self.assertEqual(data["title"], "Admin session")
        self.assertEqual(data["session_id"], self.session.session_id)

    def test_anonymous_is_blocked(self):
        resp = self.client.get(self._url())
        # admin_view redirects anonymous users to the login page.
        self.assertIn(resp.status_code, (302, 403))
        if resp.status_code == 302:
            self.assertIn("/admin/login/", resp["Location"])


class ChatApiScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.alice = User.objects.create_user(email="alice@example.com", password="x")
        self.bob = User.objects.create_user(email="bob@example.com", password="x")
        self.alice_co = Company.objects.create(owner=self.alice, name="AC", slug="ac")
        self.bob_co = Company.objects.create(owner=self.bob, name="BC", slug="bc")
        self.bob_session = ConversationSession.objects.create(
            company=self.bob_co, user=self.bob, title="Bob session"
        )

    def test_create_session_defaults_to_single_owned_company(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            "/api/v1/chat/conversation-session/",
            {"title": "Mine"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(str(resp.data["company"]), str(self.alice_co.id))
        self.assertEqual(str(resp.data["user"]), str(self.alice.id))

    def test_cannot_attach_another_owners_company(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            "/api/v1/chat/conversation-session/",
            {"title": "Hijack", "company": str(self.bob_co.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("company", resp.data)

    def test_ask_on_another_owners_session_is_404(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(
            "/api/v1/chat/conversation/ask/",
            {"session": str(self.bob_session.id), "question": "hi"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Conversation.objects.filter(session=self.bob_session).exists())


class AdminChatUITests(TestCase):
    """HTTP-layer tests for the in-admin chat UI (fake LLM, no network/key)."""

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            email="super@example.com", password="pw"
        )
        cls.alice = User.objects.create_user(
            email="alice@example.com", password="pw", is_staff=True
        )
        cls.bob = User.objects.create_user(
            email="bob@example.com", password="pw", is_staff=True
        )
        cls.alice_co = Company.objects.create(
            owner=cls.alice, name="Alice Co", slug="alice-co"
        )
        cls.bob_co = Company.objects.create(
            owner=cls.bob, name="Bob Co", slug="bob-co"
        )
        cls.alice_session = ConversationSession.objects.create(
            company=cls.alice_co, user=cls.alice, title="Alice chat"
        )
        cls.bob_session = ConversationSession.objects.create(
            company=cls.bob_co, user=cls.bob, title="Bob chat"
        )

    @staticmethod
    def _fake_service(reply="hi"):
        return mock.patch.object(
            ConversationSessionAdmin,
            "_chat_service",
            return_value=ChatService(llm_client=_FakeLLM(reply=reply)),
        )

    # --- chat page ---------------------------------------------------------
    def test_chat_page_renders_for_staff(self):
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("admin:chat_conversation_sessions"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp["Content-Type"])
        body = resp.content.decode()
        self.assertIn('id="chat-session-list"', body)
        self.assertIn('id="chat-company"', body)
        self.assertIn('id="chat-input"', body)

    def test_chat_page_extends_admin_base_with_sidebar(self):
        """The page uses the Unfold admin chrome (left nav sidebar present)."""
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("admin:chat_conversation_sessions"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # base_site -> base.html renders the Unfold navigation sidebar.
        self.assertIn('id="nav-sidebar"', body)

    def test_chat_page_company_control_in_header_region(self):
        """The company selector + New chat sit in the top header row, above
        the session-list / conversation panel (not stacked in the body top)."""
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("admin:chat_conversation_sessions"))
        body = resp.content.decode()
        header_pos = body.find('id="chat-company"')
        new_pos = body.find('id="chat-new"')
        list_pos = body.find('id="chat-session-list"')
        self.assertNotEqual(header_pos, -1)
        self.assertNotEqual(new_pos, -1)
        self.assertNotEqual(list_pos, -1)
        # Company selector and New chat come before the session list / panel.
        self.assertLess(header_pos, list_pos)
        self.assertLess(new_pos, list_pos)
        # The message input is still present.
        self.assertIn('id="chat-input"', body)

    def test_chat_page_anonymous_redirected(self):
        resp = self.client.get(reverse("admin:chat_conversation_sessions"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])

    # --- companies-list scoping -------------------------------------------
    def test_companies_json_scoped(self):
        url = reverse("admin:chat_conversation_companies_json")

        self.client.force_login(self.alice)
        names = {c["name"] for c in self.client.get(url).json()["companies"]}
        self.assertEqual(names, {"Alice Co"})

        self.client.force_login(self.bob)
        names = {c["name"] for c in self.client.get(url).json()["companies"]}
        self.assertEqual(names, {"Bob Co"})

        self.client.force_login(self.superuser)
        names = {c["name"] for c in self.client.get(url).json()["companies"]}
        self.assertEqual(names, {"Alice Co", "Bob Co"})

    # --- sessions-list scoping --------------------------------------------
    def test_sessions_json_scoped(self):
        url = reverse("admin:chat_conversation_sessions_json")

        self.client.force_login(self.alice)
        sessions = self.client.get(url).json()["sessions"]
        self.assertEqual([s["title"] for s in sessions], ["Alice chat"])

        # Alice filtering by Bob's company sees nothing (not Bob's sessions).
        sessions = self.client.get(
            url, {"company": str(self.bob_co.id)}
        ).json()["sessions"]
        self.assertEqual(sessions, [])

        # Superuser filtered by a company returns that company's sessions.
        self.client.force_login(self.superuser)
        sessions = self.client.get(
            url, {"company": str(self.bob_co.id)}
        ).json()["sessions"]
        self.assertEqual([s["title"] for s in sessions], ["Bob chat"])

    # --- turns-json scoping ------------------------------------------------
    def test_turns_json_out_of_scope_is_404(self):
        self.client.force_login(self.alice)
        url = reverse(
            "admin:chat_conversation_turns_json", args=[self.bob_session.pk]
        )
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_turns_json_in_scope_ok(self):
        self.client.force_login(self.alice)
        url = reverse(
            "admin:chat_conversation_turns_json", args=[self.alice_session.pk]
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title"], "Alice chat")
        self.assertEqual(data["company_id"], str(self.alice_co.id))

    # --- new-session creation ---------------------------------------------
    def test_new_session_scoped_create(self):
        self.client.force_login(self.alice)
        url = reverse("admin:chat_conversation_new")

        resp = self.client.post(url, {"company": str(self.alice_co.id)})
        self.assertEqual(resp.status_code, 201)
        # Success is strictly JSON, never a redirect (zero full-page nav).
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertIn("session_id", resp.json())
        created = ConversationSession.objects.filter(
            user=self.alice, company=self.alice_co, title="New chat"
        )
        self.assertEqual(created.count(), 1)

        # Alice cannot create under Bob's company.
        before = ConversationSession.objects.filter(company=self.bob_co).count()
        resp = self.client.post(url, {"company": str(self.bob_co.id)})
        self.assertEqual(resp.status_code, 404)
        after = ConversationSession.objects.filter(company=self.bob_co).count()
        self.assertEqual(before, after)

    def test_new_session_returns_json_never_redirects(self):
        """new_session_view always returns JSON, never an HTTP redirect.

        A GET (previously the redirecting branch) and the no-company failure
        case must both be JSON with no 3xx navigation.
        """
        url = reverse("admin:chat_conversation_new")

        # Success via GET: JSON 201, not a redirect.
        self.client.force_login(self.alice)
        resp = self.client.get(url, {"company": str(self.alice_co.id)})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertNotIn("Location", resp)
        self.assertIn("session_id", resp.json())

        # Failure (user owns no company): JSON 400 error, not a redirect.
        no_co_user = User.objects.create_user(
            email="nocompany@example.com", password="pw", is_staff=True
        )
        self.client.force_login(no_co_user)
        resp = self.client.post(url, {})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertNotIn("Location", resp)
        self.assertIn("error", resp.json())

    # --- send-message ------------------------------------------------------
    def test_send_message_persists_turn(self):
        self.client.force_login(self.alice)
        url = reverse(
            "admin:chat_conversation_send", args=[self.alice_session.pk]
        )
        with self._fake_service(reply="hello there"):
            resp = self.client.post(url, {"question": "hi"})
        self.assertEqual(resp.status_code, 201)
        turn = resp.json()["turn"]
        self.assertEqual(turn["question"], "hi")
        self.assertEqual(turn["response"], "hello there")

        rows = Conversation.objects.filter(session=self.alice_session)
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertEqual(row.company_id, self.alice_co.id)
        self.assertEqual(row.created_by_id, self.alice.id)
        self.assertEqual(row.metadata["provider"], "fake")

    def test_send_message_out_of_scope_is_404(self):
        self.client.force_login(self.alice)
        url = reverse(
            "admin:chat_conversation_send", args=[self.bob_session.pk]
        )
        with self._fake_service(reply="nope"):
            resp = self.client.post(url, {"question": "hi"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(
            Conversation.objects.filter(session=self.bob_session).exists()
        )

    def test_send_message_missing_key_returns_503(self):
        self.client.force_login(self.alice)
        url = reverse(
            "admin:chat_conversation_send", args=[self.alice_session.pk]
        )
        # A client that raises LLMConfigurationError (as a key-less real client
        # would) must surface as a clean 503, and the DB must stay uncorrupted.
        with mock.patch.object(
            ConversationSessionAdmin,
            "_chat_service",
            return_value=ChatService(llm_client=_UnconfiguredLLM()),
        ):
            resp = self.client.post(url, {"question": "hi"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("error", resp.json())
        self.assertFalse(
            Conversation.objects.filter(session=self.alice_session).exists()
        )


# =====================================================================
# Phase-3 chat rebuild (FEAT-003): Message model + company invariant,
# ChatService.process_message with a fake LLM, and the top-level /chat/
# UI + AJAX surface. No LLM key / no network: a structured fake client +
# a tmp CHROMA_PATH / MEDIA_ROOT.
# =====================================================================


class MessageModelTests(TestCase):
    """Message/MessageVersion invariants (ULID PK, company match, uniqueness)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="m@example.com", password="x")
        cls.owner = User.objects.create_user(email="mo@example.com", password="x")
        cls.company_a = Company.objects.create(owner=cls.owner, name="MA", slug="ma")
        cls.company_b = Company.objects.create(owner=cls.owner, name="MB", slug="mb")
        cls.session = ConversationSession.objects.create(
            company=cls.company_a, user=cls.user, title="S"
        )

    def test_message_save_rejects_company_mismatch(self):
        message = Message(
            conversation=self.session,
            company=self.company_b,  # mismatches session.company (A)
            role=Message.ROLE_USER,
            content="hi",
        )
        with self.assertRaises(ValidationError):
            message.save()

    def test_message_save_accepts_matching_company(self):
        message = Message(
            conversation=self.session,
            company=self.company_a,
            role=Message.ROLE_USER,
            content="hi",
        )
        message.save()
        self.assertIsNotNone(message.pk)

    def test_message_id_is_26_char_ulid(self):
        message = Message.objects.create(
            conversation=self.session,
            company=self.company_a,
            role=Message.ROLE_ASSISTANT,
            content="answer",
        )
        self.assertEqual(len(str(message.id)), 26)

    def test_message_version_unique_message_version(self):
        from django.db import IntegrityError, transaction

        message = Message.objects.create(
            conversation=self.session,
            company=self.company_a,
            role=Message.ROLE_ASSISTANT,
            content="answer",
        )
        MessageVersion.objects.create(
            company=self.company_a, message=message, content="v1", version=1
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MessageVersion.objects.create(
                    company=self.company_a, message=message, content="dup", version=1
                )


@override_settings(CHROMA_PATH=tempfile.mkdtemp(prefix="chat-chroma-"))
class ProcessMessageTests(TestCase):
    """ChatService.process_message with the structured fake client."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="p@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="PC", slug="pc"
        )

    def setUp(self):
        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user
        )

    def _service(self, response="fake answer", chart=None):
        return ChatService(llm_client=_FakeStructuredLLM(response, chart))

    def _no_chunks(self):
        """Patch retrieval so no embeddings/Chroma is touched."""
        return mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[],
        )

    def test_persists_one_user_and_one_assistant_message(self):
        with self._no_chunks():
            assistant = self._service(response="hello back").process_message(
                user=self.user,
                message_text="hi there",
                conversation=self.session,
                company=self.company,
            )
        self.assertEqual(assistant.role, Message.ROLE_ASSISTANT)
        self.assertEqual(assistant.content, "hello back")
        roles = list(
            self.session.messages.order_by("created_at").values_list(
                "role", flat=True
            )
        )
        self.assertEqual(roles, [Message.ROLE_USER, Message.ROLE_ASSISTANT])
        self.assertEqual(assistant.company_id, self.company.id)

    def test_empty_vector_store_yields_answer_not_retrieval_error(self):
        """ADDITION A: with NO library docs / a never-created ('missing')
        Chroma collection, retrieval must degrade to zero chunks and the chat
        must still return a normal LLM answer (no citations), never a
        ChatRetrievalError 502.

        This exercises the REAL retrieval path (NOT mocked) against the fresh
        empty CHROMA_PATH of this test class; the fake LLM's embed_query avoids
        any network while get_collection('pdf_chunks') is genuinely missing.
        """
        assistant = self._service(response="general answer").process_message(
            user=self.user,
            message_text="what is the capital of France?",
            conversation=self.session,
            company=self.company,
        )
        self.assertEqual(assistant.role, Message.ROLE_ASSISTANT)
        self.assertEqual(assistant.content, "general answer")
        # No documents indexed => no citations, but a real answer persisted.
        self.assertEqual(assistant.citations, [])
        roles = list(
            self.session.messages.order_by("created_at").values_list(
                "role", flat=True
            )
        )
        self.assertEqual(roles, [Message.ROLE_USER, Message.ROLE_ASSISTANT])

    def test_first_message_titles_session_bug2(self):
        """BUG 2: the first send retitles 'New session' from the message."""
        self.assertEqual(self.session.title, "New session")
        with self._no_chunks():
            self._service().process_message(
                user=self.user,
                message_text="What is the quarterly revenue breakdown?",
                conversation=self.session,
                company=self.company,
            )
        self.session.refresh_from_db()
        self.assertEqual(
            self.session.title, "What is the quarterly revenue breakdown?"[:50]
        )
        self.assertNotEqual(self.session.title, "New session")

    def test_citations_assembled_from_chunks(self):
        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            title="report.pdf",
            file_type="pdf",
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            company=self.company,
            document=document,
            chunk_index=0,
            page_number=3,
            source_location="page 3",
            content="Revenue grew 20% year over year.",
        )
        with mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[chunk],
        ):
            assistant = self._service(response="It grew [%s]." % chunk.pk).process_message(
                user=self.user,
                message_text="How did revenue change?",
                conversation=self.session,
                company=self.company,
            )
        self.assertEqual(len(assistant.citations), 1)
        citation = assistant.citations[0]
        self.assertEqual(citation["reference_id"], str(chunk.pk))
        self.assertEqual(citation["document_id"], str(document.id))
        self.assertEqual(citation["page"], 3)
        self.assertIn("Revenue grew", citation["excerpt"])

    def test_repeated_question_is_cache_hit_without_llm_call(self):
        fake = _FakeStructuredLLM(response="cached answer")
        service = ChatService(llm_client=fake)
        with self._no_chunks():
            service.process_message(
                user=self.user,
                message_text="same question",
                conversation=self.session,
                company=self.company,
            )
            self.assertEqual(fake.structured_calls, 1)
            # Identical question + unchanged document context => cache HIT.
            service.process_message(
                user=self.user,
                message_text="same question",
                conversation=self.session,
                company=self.company,
            )
        # No second LLM call: the answer came from the per-session cache.
        self.assertEqual(fake.structured_calls, 1)
        answers = self.session.messages.filter(role=Message.ROLE_ASSISTANT)
        self.assertEqual(answers.count(), 2)
        self.assertEqual({a.content for a in answers}, {"cached answer"})

    def test_cache_miss_after_signature_change(self):
        fake = _FakeStructuredLLM(response="first answer")
        service = ChatService(llm_client=fake)
        with self._no_chunks():
            service.process_message(
                user=self.user,
                message_text="same question",
                conversation=self.session,
                company=self.company,
            )
            self.assertEqual(fake.structured_calls, 1)
            # Adding a ready library doc changes the context signature => MISS.
            Document.objects.create(
                company=self.company,
                uploaded_by=self.user,
                source=Document.SOURCE_LIBRARY,
                status=Document.STATUS_READY,
                title="new.pdf",
                file_type="pdf",
                chunk_count=2,
            )
            service.process_message(
                user=self.user,
                message_text="same question",
                conversation=self.session,
                company=self.company,
            )
        # Signature changed, so the LLM was called a second time.
        self.assertEqual(fake.structured_calls, 2)

    def test_prompt_no_file_vs_file_branch(self):
        # No-file branch: the fake records a prompt mentioning general knowledge.
        no_file = _FakeStructuredLLM(response="answer")
        with self._no_chunks():
            ChatService(llm_client=no_file).process_message(
                user=self.user,
                message_text="draw a chart of sales",
                conversation=self.session,
                company=self.company,
            )
        self.assertIn("general knowledge", no_file.prompts[0])

        # File branch: with retrieved chunks the prompt forbids outside numbers.
        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            source=Document.SOURCE_LIBRARY,
            status=Document.STATUS_READY,
            title="sales.pdf",
            file_type="pdf",
            chunk_count=1,
        )
        chunk = DocumentChunk.objects.create(
            company=self.company,
            document=document,
            chunk_index=0,
            page_number=1,
            source_location="page 1",
            content="Q1 sales were 100 units.",
        )
        with_file = _FakeStructuredLLM(response="answer")
        with mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[chunk],
        ):
            ChatService(llm_client=with_file).process_message(
                user=self.user,
                message_text="draw a chart of sales",
                conversation=ConversationSession.objects.create(
                    company=self.company, user=self.user
                ),
                company=self.company,
            )
        prompt = with_file.prompts[0]
        self.assertIn("never make up numbers", prompt)
        self.assertIn("Q1 sales were 100 units.", prompt)

    @override_settings(MEDIA_ROOT=_MEDIA)
    def test_csv_distribution_chart_fallback_without_key(self):
        """A chat-attached CSV yields a deterministic chart with no LLM chart."""
        csv_doc = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            session=self.session,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            title="people.csv",
            file=SimpleUploadedFile(
                "people.csv",
                b"name,city\nAlice,NYC\nBob,LA\nCara,NYC\n",
                content_type="text/csv",
            ),
            file_type="csv",
        )
        self.assertTrue(csv_doc.file)
        # The fake LLM returns NO chart, so the deterministic CSV builder fires.
        with self._no_chunks():
            assistant = self._service(
                response="here you go"
            ).process_message(
                user=self.user,
                message_text="make a bar chart of city distribution",
                conversation=self.session,
                company=self.company,
            )
        self.assertIsNotNone(assistant.chart_data)
        self.assertIn(assistant.chart_data["type"], {"bar", "pie", "line"})
        # The CSV fallback emits a single-series chart whose points carry the
        # numeric value under a "values" LIST (not a singular "value"); pin that
        # shape so the client renderer (which reads values[0]) stays in sync.
        self.assertEqual(len(assistant.chart_data["series"]), 1)
        points = {point["label"]: point for point in assistant.chart_data["data"]}
        self.assertIn("NYC", points)
        nyc = points["NYC"]
        self.assertNotIn("value", nyc)
        self.assertIsInstance(nyc["values"], list)
        self.assertEqual(len(nyc["values"]), 1)
        # NYC appears twice in the CSV, LA once.
        self.assertEqual(nyc["values"][0], 2)
        self.assertEqual(points["LA"]["values"][0], 1)

    @override_settings(MEDIA_ROOT=_MEDIA)
    def test_attached_csv_chart_overrides_llm_fabricated_chart(self):
        """A chat-attached CSV is AUTHORITATIVE for chart requests.

        Even when the LLM returns its OWN (fabricated general-knowledge) chart,
        the persisted chart must reflect the CSV's REAL rows (NYC=10, LA=5,
        SF=3) and be a pie chart (the user asked for a pie). This proves the
        attached CSV wins over the LLM chart.
        """
        Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            session=self.session,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            title="cities.csv",
            file=SimpleUploadedFile(
                "cities.csv",
                b"city,count\nNYC,10\nLA,5\nSF,3\n",
                content_type="text/csv",
            ),
            file_type="csv",
        )
        # The fake LLM fabricates a DIFFERENT chart from "general knowledge".
        fabricated = ChartData(
            type="pie",
            title="City populations (general knowledge)",
            x_axis="city",
            y_axis="population",
            series=["Population"],
            data=[
                ChartPoint(label="NYC", values=[8000000.0]),
                ChartPoint(label="LA", values=[4000000.0]),
                ChartPoint(label="SF", values=[800000.0]),
            ],
        )
        with self._no_chunks():
            assistant = self._service(
                response="Here is a chart of city populations.",
                chart=fabricated,
            ).process_message(
                user=self.user,
                message_text="make a pie chart of count by city",
                conversation=self.session,
                company=self.company,
            )
        self.assertIsNotNone(assistant.chart_data)
        self.assertEqual(assistant.chart_data["type"], "pie")
        points = {p["label"]: p["values"][0] for p in assistant.chart_data["data"]}
        # Real CSV rows win over the LLM's fabricated populations.
        self.assertEqual(points, {"NYC": 10.0, "LA": 5.0, "SF": 3.0})

    @override_settings(MEDIA_ROOT=_MEDIA)
    def test_attached_text_content_reaches_prompt(self):
        """An attached notes.txt is injected into the prompt (strict mode on).

        The fake LLM captures the prompt it is handed; the attachment block
        (``Attached file: notes.txt`` + its text) must be present, and the
        strict file-context instruction (no outside numbers) must be active.
        """
        Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            session=self.session,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            title="notes.txt",
            file=SimpleUploadedFile(
                "notes.txt",
                b"The capital of France is Paris.",
                content_type="text/plain",
            ),
            file_type="txt",
        )
        fake = _FakeStructuredLLM(response="The capital of France is Paris.")
        with self._no_chunks():
            ChatService(llm_client=fake).process_message(
                user=self.user,
                message_text="According to my attached file, what is the capital of France?",
                conversation=self.session,
                company=self.company,
            )
        prompt = fake.prompts[0]
        self.assertIn("Attached file: notes.txt", prompt)
        self.assertIn("The capital of France is Paris.", prompt)
        # Attachment present => strict (no outside numbers) file-context branch.
        self.assertIn("never make up numbers", prompt)

    @override_settings(MEDIA_ROOT=_MEDIA)
    @skipUnless(
        document_services._tesseract_available(),
        "tesseract OCR not available",
    )
    def test_attached_image_ocr_text_reaches_prompt(self):
        """An attached PNG is OCR'd and its text injected into the prompt.

        Replicates OLD's image behaviour exactly: images are OCR'd to text via
        tesseract (no vision model) and answered from that text.
        """
        from io import BytesIO

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (320, 80), color="white")
        draw = ImageDraw.Draw(image)
        draw.text((10, 30), "INVOICE TOTAL 4242", fill="black")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)

        document = Document.objects.create(
            company=self.company,
            uploaded_by=self.user,
            session=self.session,
            source=Document.SOURCE_CHAT,
            status=Document.STATUS_READY,
            title="invoice.png",
            file=SimpleUploadedFile(
                "invoice.png", buffer.read(), content_type="image/png"
            ),
            file_type="png",
        )
        # The extractor OCRs the image to text.
        extracted = document_services.extract_document_text(document)
        self.assertIn("4242", extracted)

        fake = _FakeStructuredLLM(response="The number is 4242.")
        with self._no_chunks():
            ChatService(llm_client=fake).process_message(
                user=self.user,
                message_text="what number is in this image?",
                conversation=self.session,
                company=self.company,
            )
        prompt = fake.prompts[0]
        self.assertIn("Attached file: invoice.png", prompt)
        self.assertIn("4242", prompt)


class ChatUIViewTests(TestCase):
    """HTTP tests for the top-level /chat/ surface (FEAT-002 behaviour)."""

    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user(
            email="uialice@example.com", password="pw"
        )
        cls.bob = User.objects.create_user(
            email="uibob@example.com", password="pw"
        )
        cls.alice_co = Company.objects.create(
            owner=cls.alice, name="UI Alice Co", slug="ui-alice-co"
        )
        cls.bob_co = Company.objects.create(
            owner=cls.bob, name="UI Bob Co", slug="ui-bob-co"
        )
        cls.alice_session = ConversationSession.objects.create(
            company=cls.alice_co, user=cls.alice, title="Alice UI chat"
        )
        cls.bob_session = ConversationSession.objects.create(
            company=cls.bob_co, user=cls.bob, title="Bob UI chat"
        )

    # --- page render ------------------------------------------------------
    def test_chat_home_renders_sidebar_and_regions(self):
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("chat:home"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('id="nav-sidebar"', body)  # Unfold LEFT sidebar
        self.assertIn('id="chat-session-list"', body)  # session list region
        self.assertIn('id="chat-messages"', body)  # conversation panel region
        self.assertIn('id="chat-input"', body)  # composer region

    def test_chat_session_preselects_valid_ulid(self):
        self.client.force_login(self.alice)
        resp = self.client.get(
            reverse("chat:session", args=[self.alice_session.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(str(self.alice_session.id), resp.content.decode())

    def test_chat_session_malformed_id_is_404(self):
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("chat:session", args=["too-short"]))
        self.assertEqual(resp.status_code, 404)

    def test_chat_session_out_of_scope_is_404(self):
        self.client.force_login(self.alice)
        resp = self.client.get(
            reverse("chat:session", args=[self.bob_session.pk])
        )
        self.assertEqual(resp.status_code, 404)

    # --- new_session (BUG 1) ---------------------------------------------
    def test_new_session_returns_json_201_not_redirect_single_session(self):
        self.client.force_login(self.alice)
        before = ConversationSession.objects.filter(company=self.alice_co).count()
        resp = self.client.post(reverse("chat:new_session"))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertNotIn("Location", resp)  # never a redirect
        self.assertIn("session_id", resp.json())
        after = ConversationSession.objects.filter(company=self.alice_co).count()
        self.assertEqual(after - before, 1)  # exactly one session created

    def test_new_session_rejects_get(self):
        self.client.force_login(self.alice)
        resp = self.client.get(reverse("chat:new_session"))
        self.assertEqual(resp.status_code, 405)

    # --- send (persistence + BUG 2) --------------------------------------
    def test_send_persists_user_and_assistant_messages(self):
        self.client.force_login(self.alice)
        fake = ChatService(llm_client=_FakeStructuredLLM(response="hi back"))
        with mock.patch("chat.views._chat_service", return_value=fake), mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[],
        ):
            resp = self.client.post(
                reverse("chat:send", args=[self.alice_session.pk]),
                {"question": "hello"},
            )
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["user"]["content"], "hello")
        self.assertEqual(data["assistant"]["content"], "hi back")
        rows = Message.objects.filter(conversation=self.alice_session)
        self.assertEqual(rows.count(), 2)

    def test_send_first_message_sets_meaningful_title_bug2(self):
        session = ConversationSession.objects.create(
            company=self.alice_co, user=self.alice
        )
        self.assertEqual(session.title, "New session")
        self.client.force_login(self.alice)
        fake = ChatService(llm_client=_FakeStructuredLLM(response="ok"))
        with mock.patch("chat.views._chat_service", return_value=fake), mock.patch(
            "chat.services.document_services.find_relevant_chunks",
            return_value=[],
        ):
            resp = self.client.post(
                reverse("chat:send", args=[session.pk]),
                {"question": "Summarize the onboarding policy please"},
            )
        self.assertEqual(resp.status_code, 201)
        session.refresh_from_db()
        self.assertEqual(
            session.title, "Summarize the onboarding policy please"[:50]
        )
        self.assertNotEqual(session.title, "New session")
        self.assertEqual(resp.json()["session"]["title"], session.title)

    # --- company scoping --------------------------------------------------
    def test_send_on_another_owners_session_is_404(self):
        self.client.force_login(self.alice)
        fake = ChatService(llm_client=_FakeStructuredLLM(response="nope"))
        with mock.patch("chat.views._chat_service", return_value=fake):
            resp = self.client.post(
                reverse("chat:send", args=[self.bob_session.pk]),
                {"question": "hi"},
            )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(
            Message.objects.filter(conversation=self.bob_session).exists()
        )

    def test_messages_json_out_of_scope_is_404(self):
        self.client.force_login(self.alice)
        resp = self.client.get(
            reverse("chat:messages_json", args=[self.bob_session.pk])
        )
        self.assertEqual(resp.status_code, 404)

    # --- attach -----------------------------------------------------------
    @override_settings(MEDIA_ROOT=_MEDIA)
    def test_attach_creates_chat_source_document(self):
        self.client.force_login(self.alice)
        upload = SimpleUploadedFile(
            "notes.txt", b"hello world", content_type="text/plain"
        )
        resp = self.client.post(
            reverse("chat:attach", args=[self.alice_session.pk]),
            {"file": upload},
        )
        self.assertEqual(resp.status_code, 201)
        doc_id = resp.json()["id"]
        document = Document.objects.get(pk=doc_id)
        self.assertEqual(document.source, Document.SOURCE_CHAT)
        self.assertEqual(document.status, Document.STATUS_READY)
        self.assertEqual(document.session_id, self.alice_session.pk)
        self.assertEqual(document.company_id, self.alice_co.id)
        self.assertEqual(document.uploaded_by_id, self.alice.id)

    # --- delete_session (soft-delete) ------------------------------------
    def test_delete_session_soft_deletes_in_scope(self):
        session = ConversationSession.objects.create(
            company=self.alice_co, user=self.alice, title="To delete"
        )
        Message.objects.create(
            conversation=session,
            company=self.alice_co,
            role=Message.ROLE_USER,
            content="hi",
        )
        self.client.force_login(self.alice)
        resp = self.client.post(
            reverse("chat:delete_session", args=[session.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertTrue(resp.json()["deleted"])

        # Soft-deleted (is_deleted=True), not hard-deleted.
        self.assertFalse(
            ConversationSession.objects.filter(pk=session.pk).exists()
        )
        row = ConversationSession.all_objects.get(pk=session.pk)
        self.assertTrue(row.is_deleted)
        self.assertIsNotNone(row.deleted_at)

        # The session's messages are soft-deleted too.
        self.assertFalse(
            Message.objects.filter(conversation=session).exists()
        )
        self.assertTrue(
            Message.all_objects.filter(conversation=session, is_deleted=True).exists()
        )

        # It disappears from the sessions list.
        listing = self.client.get(reverse("chat:sessions_json")).json()
        ids = {s["id"] for s in listing["sessions"]}
        self.assertNotIn(str(session.pk), ids)

    def test_delete_session_out_of_scope_is_404(self):
        self.client.force_login(self.alice)
        resp = self.client.post(
            reverse("chat:delete_session", args=[self.bob_session.pk])
        )
        self.assertEqual(resp.status_code, 404)
        # Bob's session is untouched (still visible, not deleted).
        self.assertTrue(
            ConversationSession.objects.filter(pk=self.bob_session.pk).exists()
        )

    def test_delete_session_rejects_get(self):
        session = ConversationSession.objects.create(
            company=self.alice_co, user=self.alice, title="Keep"
        )
        self.client.force_login(self.alice)
        resp = self.client.get(
            reverse("chat:delete_session", args=[session.pk])
        )
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(
            ConversationSession.objects.filter(pk=session.pk).exists()
        )

    def test_delete_session_enforces_csrf(self):
        session = ConversationSession.objects.create(
            company=self.alice_co, user=self.alice, title="CSRF"
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.alice)
        resp = csrf_client.post(
            reverse("chat:delete_session", args=[session.pk])
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            ConversationSession.objects.filter(pk=session.pk).exists()
        )

    # --- rendered page structural assertions (BUG 1/2/delete button) -----
    def test_chat_page_has_no_leaked_template_comments(self):
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        self.assertNotIn("Force a side-by-side", body)
        self.assertNotIn("composer is visible by default", body)

    def test_chat_page_composer_is_bottom_of_flex_column_panel(self):
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        # The composer markup is present.
        self.assertIn('id="chat-composer"', body)
        # #chat-messages (grow/scroll) comes before #chat-composer (bottom row)
        # within the flex-column panel.
        messages_pos = body.find('id="chat-messages"')
        composer_pos = body.find('id="chat-composer"')
        self.assertNotEqual(messages_pos, -1)
        self.assertNotEqual(composer_pos, -1)
        self.assertLess(messages_pos, composer_pos)
        # The growing/scrolling transcript area.
        self.assertIn("overflow-y-auto", body)
        # The composer is pinned to the bottom (margin-top:auto) as the last
        # child of the flex-column panel.
        composer_tag = body[composer_pos:composer_pos + 120]
        self.assertIn("mt-auto", composer_tag)
        # The attach control renders as an SVG icon, NOT the literal ligature
        # text "attach_file".
        self.assertNotIn("attach_file", body)

    def test_chat_page_layout_is_viewport_bounded(self):
        """The chat layout must be bounded to the viewport (DEFINITE height +
        overflow hidden) so the page never grows past the screen and pushes the
        composer below the fold. The transcript scrolls INTERNALLY instead."""
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        layout_pos = body.find('id="chat-layout"')
        self.assertNotEqual(layout_pos, -1)
        layout_tag = body[layout_pos:layout_pos + 260]
        # Bounded to the viewport with a definite height (calc(100vh - header))
        # and overflow hidden on the outer layout; children scroll internally.
        self.assertIn("calc(100vh - 12rem)", layout_tag)
        self.assertIn("height:", layout_tag)
        self.assertIn("overflow:hidden", layout_tag)
        # The transcript area grows + scrolls internally (flex-1 + min-h-0 +
        # overflow-y-auto); it is NOT the whole page scrolling.
        messages_pos = body.find('id="chat-messages"')
        self.assertNotEqual(messages_pos, -1)
        messages_tag = body[messages_pos:messages_pos + 120]
        self.assertIn("flex-1", messages_tag)
        self.assertIn("min-h-0", messages_tag)
        self.assertIn("overflow-y-auto", messages_tag)

    def test_chat_page_title_header_has_top_right_delete_control(self):
        """The OPEN conversation's title header carries a top-right delete
        control that REUSES the existing /chat/<id>/delete/ endpoint (no new
        endpoint). The icon renders as SVG, not the literal word 'delete'."""
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        # The conversation panel has a title/header row with a delete button.
        header_pos = body.find('id="chat-conv-header"')
        title_pos = body.find('id="chat-conv-title"')
        delete_pos = body.find('id="chat-conv-delete"')
        self.assertNotEqual(header_pos, -1)
        self.assertNotEqual(title_pos, -1)
        self.assertNotEqual(delete_pos, -1)
        # The title header sits ABOVE the messages area (fixed top row), and the
        # title comes before the delete control (delete is at the top-RIGHT).
        self.assertLess(header_pos, body.find('id="chat-messages"'))
        self.assertLess(title_pos, delete_pos)
        # The delete control is NOT the literal ligature word; the icon is
        # injected as an inline SVG client-side (makeTrashIcon via createElementNS).
        delete_tag = body[delete_pos:delete_pos + 400]
        self.assertNotIn(">delete<", delete_tag)
        self.assertIn("makeTrashIcon", body)
        # It reuses the shared deleteSession() flow -> the existing delete
        # endpoint; the per-session delete URL fragment is present in the JS.
        self.assertIn('chat-conv-delete', body)
        self.assertIn('"/delete/"', body)

    def test_chat_page_renders_delete_control_wiring(self):
        """Session rows get a delete control (rendered client-side). Assert the
        page ships the delete wiring: the trash marker + the delete endpoint."""
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        self.assertIn("data-delete-session-id", body)
        self.assertIn("/delete/", body)

    def test_chat_page_icons_are_svg_not_literal_ligature_text(self):
        """Icons must render as SVG, not the literal Material Symbols ligature
        word (the user saw the paperclip render as the text 'attach_file')."""
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        # The attach control is an inline SVG, so the ligature word must NOT be
        # present as static markup text.
        self.assertNotIn("attach_file", body)
        self.assertNotIn(">add<", body)
        # Inline SVGs are present for the composer/new-chat controls.
        self.assertIn("<svg", body)
        # Belt-and-suspenders: the Material Symbols font is still loaded by the
        # Unfold shell for any remaining glyphs.
        self.assertIn("fonts/material-symbols/styles.css", body)

    def test_company_selector_rendered_in_header_not_chats_column(self):
        """ADDITION B: the #chat-company select sits in the page header
        (#chat-header), before the Chats column / session list."""
        self.client.force_login(self.alice)
        body = self.client.get(reverse("chat:home")).content.decode()
        header_pos = body.find('id="chat-header"')
        company_pos = body.find('id="chat-company"')
        list_pos = body.find('id="chat-session-list"')
        self.assertNotEqual(header_pos, -1)
        self.assertNotEqual(company_pos, -1)
        self.assertNotEqual(list_pos, -1)
        # The company selector lives inside the header, above the session list.
        self.assertLess(header_pos, company_pos)
        self.assertLess(company_pos, list_pos)

    # --- ADDITION A: empty/missing vector store must NOT hard-fail --------
    @override_settings(CHROMA_PATH=tempfile.mkdtemp(prefix="chat-empty-chroma-"))
    def test_send_with_empty_vector_store_returns_answer(self):
        """With NO library documents (empty/absent Chroma collection), a send
        must still return a normal assistant answer (no citations), not a 502.

        Uses the structured fake LLM (its embed_query avoids any network/key) and
        the REAL retrieval path (find_relevant_chunks is NOT mocked), so the
        empty-collection handling in ChromaVectorStore is exercised end-to-end.
        """
        session = ConversationSession.objects.create(
            company=self.alice_co, user=self.alice
        )
        self.client.force_login(self.alice)
        fake = ChatService(llm_client=_FakeStructuredLLM(response="general answer"))
        with mock.patch("chat.views._chat_service", return_value=fake):
            resp = self.client.post(
                reverse("chat:send", args=[session.pk]),
                {"question": "What is the capital of France?"},
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["assistant"]["content"], "general answer")
        self.assertEqual(data["assistant"]["citations"], [])
        # Both the user and assistant messages were persisted.
        roles = list(
            session.messages.order_by("created_at").values_list("role", flat=True)
        )
        self.assertEqual(roles, [Message.ROLE_USER, Message.ROLE_ASSISTANT])

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)
