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

    def _ensure_client(self):
        if self._client is None:
            if not self._api_key:
                raise LLMConfigurationError("No LLM API key configured.")


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
        """Return a ``schema`` instance validated from a structured completion."""
        client = self._ensure_client()
        try:
            structured_client = client.with_structured_output(schema)
            response = structured_client.invoke(prompt)
        except Exception as exc:
            logger.error(
                "LLM structured call failed (provider=%s model=%s)",
                self.provider,
                self.model,
            )
            raise LLMBackendError("LLM structured call failed.") from exc
        if isinstance(response, schema):
            return response
        return schema.model_validate(response)

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


def get_llm_client(provider: str | None = None) -> LLMClient:
    """Return an LLMClient for ``provider`` (default: settings.LLM_PROVIDER).

    Pass provider="openai" or "deepseek" to switch the WHOLE client (chat model,
    embedding model, base_url, key) for that call. Builds no backend at import or
    call time (LLMClient is lazy); tests patch this function to inject a fake.
    """
    return LLMClient(provider=provider)
