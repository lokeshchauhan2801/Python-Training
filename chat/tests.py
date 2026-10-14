"""Targeted tests for the chat domain (FEAT-003 + FEAT-004).

Covers the company-match invariant on Conversation.save() and ChatService using
an injected fake LLM client (no network, no key), plus the admin AJAX
``detail-json`` endpoint (authenticated JSON, anonymous blocked) and the DRF
owner/company scoping (a user cannot attach or ``ask`` on another owner's
company/session).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from basics.llm import LLMConfigurationError
from company.models import Company

from .admin import ConversationSessionAdmin
from .models import Conversation, ConversationSession
from .services import ChatService

User = get_user_model()


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
