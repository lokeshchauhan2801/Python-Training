"""Pure-Python tests for the model->provider resolver in basics.llm.

These are SimpleTestCases (no DB): resolve_provider_model only reads
settings.LLM_PROVIDERS / LLM_PROVIDER and returns a plain tuple.
"""

from django.conf import settings
from django.test import SimpleTestCase

from basics.llm import resolve_provider_model


class ResolveProviderModelTests(SimpleTestCase):
    def test_openai_model_resolves_to_openai(self):
        self.assertEqual(
            resolve_provider_model("gpt-4o-mini"), ("openai", "gpt-4o-mini")
        )
        self.assertEqual(resolve_provider_model("gpt-4o"), ("openai", "gpt-4o"))

    def test_deepseek_model_resolves_to_deepseek(self):
        self.assertEqual(
            resolve_provider_model("deepseek-chat"), ("deepseek", "deepseek-chat")
        )
        self.assertEqual(
            resolve_provider_model("deepseek-reasoner"),
            ("deepseek", "deepseek-reasoner"),
        )

    def test_explicit_provider_wins_and_disambiguates(self):


        provider, model = resolve_provider_model("gpt-4o-mini", provider="deepseek")
        self.assertEqual(provider, "deepseek")
        self.assertEqual(model, "gpt-4o-mini")

    def test_explicit_provider_without_model_uses_its_default(self):
        provider, model = resolve_provider_model(None, provider="deepseek")
        self.assertEqual(provider, "deepseek")
        self.assertEqual(
            model, settings.LLM_PROVIDERS["deepseek"]["chat_model"]
        )

    def test_unknown_model_falls_back_to_default_provider(self):
        provider, model = resolve_provider_model("not-a-real-model")
        self.assertEqual(provider, settings.LLM_PROVIDER)
        default_cfg = settings.LLM_PROVIDERS[settings.LLM_PROVIDER]
        self.assertEqual(model, default_cfg["chat_model"])

    def test_empty_selection_falls_back_to_default_provider_and_model(self):
        provider, model = resolve_provider_model(None, None)
        self.assertEqual(provider, settings.LLM_PROVIDER)
        default_cfg = settings.LLM_PROVIDERS[settings.LLM_PROVIDER]
        self.assertEqual(model, default_cfg["chat_model"])

    def test_unknown_explicit_provider_is_ignored(self):

        self.assertEqual(
            resolve_provider_model("deepseek-chat", provider="bogus"),
            ("deepseek", "deepseek-chat"),
        )
