"""Targeted tests for the chat domain (FEAT-003).

Covers the company-match invariant on Conversation.save() and ChatService using
an injected fake LLM client (no network, no key).
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

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
