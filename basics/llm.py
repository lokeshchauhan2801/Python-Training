"""Provider-agnostic, injectable LLM adapter.

``LLMClient`` wraps ``langchain_openai.ChatOpenAI`` but never constructs a
backend client at import or construction time. The backend is built lazily on
the first ``complete`` call, so the project imports and runs ``check``/``migrate``
with no key present. The provider is config-only (env), and ``complete`` exposes
a single narrow contract that returns a plain string regardless of the backend
content shape.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base class for all LLM adapter errors."""


class LLMConfigurationError(LLMError):
    """Raised when the client is used without a usable configuration (e.g. no key)."""


class LLMBackendError(LLMError):
    """Raised when the backend provider call fails."""


class LLMClient:
    def __init__(self, *, model=None, api_key=None, base_url=None, provider=None):
        self.provider = provider or settings.LLM_PROVIDER  # label only
        self.model = model or settings.LLM_MODEL
        self.base_url = base_url or settings.LLM_BASE_URL or None
        self._api_key = api_key or settings.LLM_API_KEY or None
        self._client = None  # lazy; NOT built at import or construct time

    def _ensure_client(self):
        if self._client is None:
            if not self._api_key:
                raise LLMConfigurationError("No LLM API key configured.")
            # Imported lazily so that merely importing this module (and running
            # check/migrate) never requires the backend to be constructable.
            from langchain_openai import ChatOpenAI

            self._client = ChatOpenAI(
                model=self.model,
                api_key=self._api_key,
                base_url=self.base_url,
            )
        return self._client

    def complete(self, messages, *, temperature=None, max_tokens=None) -> str:
        client = self._ensure_client()
        try:
            result = client.invoke(self._to_lc_messages(messages))
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error
            logger.error(
                "LLM backend call failed (provider=%s model=%s)",
                self.provider,
                self.model,
            )
            raise LLMBackendError("LLM backend call failed.") from exc
        return self._content_to_text(result.content)

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
