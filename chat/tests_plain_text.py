from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from company.models import Company
from chat.models import ConversationSession
from chat.services.chat_service import ChatService
from chat.services.llm_tools import ToolContext, _chat_summarise
from chat.services.plain_text import normalize_llm_response

User = get_user_model()


class _FakeLLM:
    provider = "fake"
    model = "fake-model"

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, messages, *, temperature=None, max_tokens=None):
        return self.response

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        return schema(response=self.response, chart=None, title=None)


class NormalizeLLMResponseTests(SimpleTestCase):
    def test_converts_prose_markdown_without_changing_protected_content(self):
        response = """**Elon Musk** is an entrepreneur.
- **CEO of Tesla**
+ SpaceX
* xAI
1. **Born** in 1971
Use `**literal**` and `2 * 3`.
JSON: {"name": "**literal**", "formula": "2 * 3"}
https://example.com/a*b
```python
value = 2 * 3
```
Keep long-term plans and calculate 2 * 3."""

        self.assertEqual(
            normalize_llm_response(response),
            """Elon Musk is an entrepreneur.
• CEO of Tesla
• SpaceX
• xAI
1. Born in 1971
Use `**literal**` and `2 * 3`.
JSON: {"name": "**literal**", "formula": "2 * 3"}
https://example.com/a*b
```python
value = 2 * 3
```
Keep long-term plans and calculate 2 * 3.""",
        )


class DirectLLMPlainTextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="plain@example.com", password="x")
        cls.company = Company.objects.create(
            owner=cls.user, name="Plain Text", slug="plain-text"
        )

    def setUp(self):
        self.session = ConversationSession.objects.create(
            company=self.company, user=self.user
        )
        self.response = "**Elon Musk**\n- **CEO of Tesla**"

    def test_ask_and_summary_persist_normalized_llm_content(self):
        service = ChatService(llm_client=_FakeLLM(self.response))

        turn = service.ask(session=self.session, user=self.user, question="Who is Elon?")
        summary = service.summarize(session=self.session, actor=self.user)
        self.session.refresh_from_db()

        self.assertEqual(turn.response, "Elon Musk\n• CEO of Tesla")
        self.assertEqual(summary, "Elon Musk\n• CEO of Tesla")
        self.assertEqual(self.session.summary, "Elon Musk\n• CEO of Tesla")

    def test_langgraph_summary_persists_normalized_llm_content(self):
        result = _chat_summarise(
            ToolContext(
                user=self.user,
                company=self.company,
                conversation=self.session,
                message_text="Summarize this chat",
                llm=_FakeLLM(self.response),
            )
        )
        self.session.refresh_from_db()

        self.assertTrue(result.llm_generated)
        self.assertEqual(result.content, "Elon Musk\n• CEO of Tesla")
        self.assertEqual(self.session.summary, "Elon Musk\n• CEO of Tesla")

    def test_process_message_persists_normalized_content_and_requests_plain_text(self):
        llm = _FakeLLM(self.response)
        service = ChatService(llm_client=llm)

        with mock.patch.object(service, "_find_relevant_chunks", return_value=[]):
            turn = service.process_message(
                user=self.user,
                message_text="Who is Elon?",
                conversation=self.session,
                company=self.company,
            )

        self.assertEqual(turn.response, "Elon Musk\n• CEO of Tesla")
        self.assertIn("Do not use Markdown emphasis or asterisk bullets", llm.prompts[0])
