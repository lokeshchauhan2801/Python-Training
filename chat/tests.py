"""Targeted tests for the chat domain (FEAT-003 + FEAT-004).

Covers the company-match invariant on Conversation.save() and ChatService using
an injected fake LLM client (no network, no key), plus the admin AJAX
``detail-json`` endpoint (authenticated JSON, anonymous blocked) and the DRF
owner/company scoping (a user cannot attach or ``ask`` on another owner's
company/session).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company

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
