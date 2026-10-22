import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base class for all LLM adapter errors."""


class LLMConfigurationError(LLMError):
    """Raised when the client is used without a usable configuration (e.g. no key)."""


class LLMBackendError(LLMError):
    """Raised when the backend provider call fails."""


def _provider_config(provider):
    """Resolve a provider's config from settings.LLM_PROVIDERS.

    Falls back to the generic LLM_*/EMBEDDING_MODEL globals when the provider is
    unknown or not in the registry, so existing single-provider setups are
    unaffected. Returns a dict with chat_model, embedding_model, base_url,
    api_key, embedding_base_url, embedding_api_key.
    """
    registry = getattr(settings, "LLM_PROVIDERS", {}) or {}
    name = provider or settings.LLM_PROVIDER
    cfg = registry.get(name)
    if cfg is None:

        return {
            "chat_model": settings.LLM_MODEL,
            "embedding_model": getattr(settings, "EMBEDDING_MODEL", None),
            "base_url": settings.LLM_BASE_URL or "",
            "api_key": settings.LLM_API_KEY or "",
            "embedding_base_url": "",
            "embedding_api_key": "",
        }
    return cfg


class LLMClient:
    def __init__(self, *, model=None, api_key=None, base_url=None, provider=None):


        self.provider = provider or settings.LLM_PROVIDER
        cfg = _provider_config(self.provider)

        self.model = model or cfg.get("chat_model") or settings.LLM_MODEL
        self.base_url = (base_url or cfg.get("base_url") or "") or None
        self._api_key = (api_key or cfg.get("api_key") or settings.LLM_API_KEY) or None


        self._embedding_model = (
            cfg.get("embedding_model") or getattr(settings, "EMBEDDING_MODEL", None)
        )
        self._embedding_base_url = (cfg.get("embedding_base_url") or "") or None
        self._embedding_api_key = (
            cfg.get("embedding_api_key")


            or (self._api_key if self._embedding_base_url == self.base_url else None)
        ) or None

        self._client = None
        self._embeddings = None

    @staticmethod
    def _clear_empty_base_url_env():
        """Drop empty OpenAI base-url env vars before building an SDK client.

        The openai SDK reads ``OPENAI_BASE_URL`` / ``OPENAI_API_BASE`` from the
        environment as a default base URL. When one of these is set to an EMPTY
        string (e.g. ``OPENAI_BASE_URL=`` in a .env loaded via python-dotenv),
        the SDK treats ``""`` as a configured endpoint and hands it to httpx,
        which rejects it with ``UnsupportedProtocol: Request URL is missing an
        'http://' or 'https://' protocol.`` — breaking EVERY embedding/chat call
        that relies on the provider default (base_url=None), notably the
        embedding path under both openai and deepseek.

        We pass an explicit ``base_url`` to the client anyway, but when that
        value is ``None`` the SDK still consults the env var, so an empty env var
        wins over our intended default. Removing only the EMPTY values (never a
        real URL a user deliberately exported) makes our resolved base_url the
        single source of truth.
        """
        import os

        for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
            if os.environ.get(name, None) == "":
                del os.environ[name]

    def _ensure_client(self):
        if self._client is None:
            if not self._api_key:
                raise LLMConfigurationError("No LLM API key configured.")
            self._clear_empty_base_url_env()


            from langchain_openai import ChatOpenAI

            self._client = ChatOpenAI(
                model=self.model,
                api_key=self._api_key,
                base_url=self.base_url,
            )
        return self._client

    def _ensure_embeddings(self):
        if self._embeddings is None:
            if not self._embedding_api_key:
                raise LLMConfigurationError(
                    "LLM API key required to embed documents."
                )
            self._clear_empty_base_url_env()


            from langchain_openai import OpenAIEmbeddings

            self._embeddings = OpenAIEmbeddings(
                model=self._embedding_model,
                api_key=self._embedding_api_key,
                base_url=self._embedding_base_url,
            )
        return self._embeddings

    def complete(self, messages, *, temperature=None, max_tokens=None) -> str:
        client = self._ensure_client()
        try:
            result = client.invoke(self._to_lc_messages(messages))
        except Exception as exc:
            logger.error(
                "LLM backend call failed (provider=%s model=%s)",
                self.provider,
                self.model,
            )
            raise LLMBackendError("LLM backend call failed.") from exc
        return self._content_to_text(result.content)

    def embed_documents(self, texts) -> list:
        embeddings = self._ensure_embeddings()
        try:
            return embeddings.embed_documents(texts)
        except Exception as exc:
            logger.error(
                "LLM embedding call failed (provider=%s model=%s)",
                self.provider,
                self._embedding_model,
            )
            raise LLMBackendError("LLM embedding call failed.") from exc

    def embed_query(self, text) -> list:
        embeddings = self._ensure_embeddings()
        try:
            return embeddings.embed_query(text)
        except Exception as exc:
            logger.error(
                "LLM embedding call failed (provider=%s model=%s)",
                self.provider,
                self._embedding_model,
            )
            raise LLMBackendError("LLM embedding call failed.") from exc

    def generate_structured(self, prompt, schema):
        """Return a ``schema`` instance validated from a structured completion.

        Tries the provider's NATIVE structured output first (OpenAI function /
        json-schema via ``with_structured_output``). Providers without that
        capability — notably DeepSeek's ``deepseek-chat`` over the OpenAI-compat
        shim — raise there; for them we FALL BACK to a plain completion that
        appends the schema and parses JSON out of the text. So structured output
        works across providers without the caller changing anything.
        """
        client = self._ensure_client()
        try:
            structured_client = client.with_structured_output(schema)
            response = structured_client.invoke(prompt)
            if isinstance(response, schema):
                return response
            return schema.model_validate(response)
        except Exception as native_exc:
            logger.info(
                "native structured output unavailable (provider=%s model=%s); "
                "falling back to prompt+parse",
                self.provider,
                self.model,
            )
            return self._structured_via_prompt(prompt, schema, native_exc)

    def _structured_via_prompt(self, prompt, schema, native_exc):
        """Prompt-and-parse structured output for providers without native support."""
        import json
        import re

        try:
            json_schema = json.dumps(schema.model_json_schema())
        except Exception:
            json_schema = "{}"

        text_prompt = prompt if isinstance(prompt, str) else str(prompt)
        instruction = (
            text_prompt
            + "\n\nReturn ONLY a single JSON object (no prose, no code fences) "
            "that validates against this JSON schema:\n" + json_schema
        )
        try:
            raw = self.complete([{"role": "user", "content": instruction}])
        except LLMError:
            raise
        except Exception as exc:
            logger.error(
                "LLM structured call failed (provider=%s model=%s)",
                self.provider,
                self.model,
            )
            raise LLMBackendError("LLM structured call failed.") from exc


        candidate = (raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
        if fenced:
            candidate = fenced.group(1).strip()
        if not candidate.startswith("{"):
            brace = candidate.find("{")
            if brace != -1:
                candidate = candidate[brace:]
        try:
            data = json.loads(candidate)
            return schema.model_validate(data)
        except Exception as parse_exc:
            logger.error(
                "LLM structured call failed (provider=%s model=%s): could not "
                "parse JSON from response",
                self.provider,
                self.model,
            )
            raise LLMBackendError(
                "LLM structured call failed."
            ) from (parse_exc or native_exc)

    @staticmethod
    def _to_lc_messages(messages):
        """Map provider-neutral role/content dicts to LangChain message objects."""
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            SystemMessage,
        )

        role_map = {
            "system": SystemMessage,
            "assistant": AIMessage,
            "ai": AIMessage,
            "user": HumanMessage,
            "human": HumanMessage,
        }
        lc_messages = []
        for message in messages:
            role = (message.get("role") or "user").lower()
            content = message.get("content", "")
            cls = role_map.get(role, HumanMessage)
            lc_messages.append(cls(content=content))
        return lc_messages

    @staticmethod
    def _content_to_text(content) -> str:
        """Collapse backend content (str | list[block]) into a single string."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)


def resolve_provider_model(model_name=None, provider=None):
    """Map a selected (model, provider) to a concrete ``(provider, model)`` pair.

    Single source of truth for turning the composer's selection into the
    provider+model the real LLM call should use. Rules (in order):

    1. An explicit, registry-known ``provider`` wins. This disambiguates a model
       id that two providers might both list; the model is kept as given (or the
       provider's chat_model/first model when no model was sent).
    2. Otherwise, scan ``settings.LLM_PROVIDERS`` for the provider whose
       ``"models"`` list (or ``"chat_model"``) contains ``model_name`` and return
       that provider with ``model_name``.
    3. Otherwise (empty/unknown model and no valid explicit provider) fall back
       to ``settings.LLM_PROVIDER`` and that provider's ``chat_model`` (else its
       first model, else ``settings.LLM_MODEL``) — preserving today's behaviour
       exactly when the client sends nothing.

    Returns a plain ``(provider_key, model_id)`` tuple and never constructs a
    backend client, so callers stay no-key-safe.
    """
    registry = getattr(settings, "LLM_PROVIDERS", {}) or {}

    def _default_model(provider_key):
        cfg = registry.get(provider_key) or {}
        return (
            cfg.get("chat_model")
            or (cfg.get("models") or [None])[0]
            or settings.LLM_MODEL
        )

    name = (model_name or "").strip() or None
    explicit = (provider or "").strip() or None


    if explicit and explicit in registry:
        return explicit, (name or _default_model(explicit))


    if name:
        for provider_key, cfg in registry.items():
            models = cfg.get("models") or []
            if name in models or name == cfg.get("chat_model"):
                return provider_key, name


    default_provider = settings.LLM_PROVIDER
    return default_provider, _default_model(default_provider)


def get_llm_client(provider: str | None = None) -> LLMClient:
    """Return an LLMClient for ``provider`` (default: settings.LLM_PROVIDER).

    Pass provider="openai" or "deepseek" to switch the WHOLE client (chat model,
    embedding model, base_url, key) for that call. Builds no backend at import or
    call time (LLMClient is lazy); tests patch this function to inject a fake.
    """
    return LLMClient(provider=provider)
