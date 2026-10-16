"""Import-safe Langfuse wrapper.

A thin adaptation layer around the Langfuse SDK that NEVER touches the SDK at
import time and is a complete no-op when Langfuse is not configured. This is the
single place that absorbs SDK v2/v3 API differences; its public surface
(``get_prompt``, ``trace``, ``flush``) is version-independent, so the rest of
the project is unaffected by whichever SDK major resolves.

Configuration: ``settings.LANGFUSE_SECRET_KEY`` / ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_BASE_URL``. If ANY is missing the wrapper is a no-op:
``get_prompt`` returns the ``fallback``, ``trace`` yields a no-op context
manager, ``flush`` does nothing, and the SDK is NEVER imported. Only key
PRESENCE is ever logged — never a key value.
"""

from __future__ import annotations

import contextlib
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class LangfuseClient:
    """Wrapper over the Langfuse SDK (or a no-op when unconfigured)."""

    def __init__(self, *, secret_key="", public_key="", base_url=""):
        self._secret_key = secret_key
        self._public_key = public_key
        self._base_url = base_url
        self._configured = bool(secret_key and public_key and base_url)
        self._client = None  # lazy; SDK is NOT imported until first use

    @property
    def configured(self) -> bool:
        return self._configured

    def _ensure_client(self):
        """Lazily construct the SDK client, tolerating v2/v3 differences.

        Returns ``None`` (and keeps the wrapper a no-op) on any import or
        construction failure, so a misbehaving SDK never breaks a chat turn.
        """
        if not self._configured:
            return None
        if self._client is None:
            try:
                import langfuse  # lazy import; only when fully configured

                self._client = langfuse.Langfuse(
                    public_key=self._public_key,
                    secret_key=self._secret_key,
                    host=self._base_url,
                )
            except Exception:  # noqa: BLE001 - SDK optional; degrade gracefully
                logger.debug(
                    "langfuse SDK unavailable or failed to initialise; "
                    "using local prompt fallback"
                )
                self._client = None
        return self._client

    def get_prompt(self, name: str, *, fallback: str) -> str:
        """Return the managed prompt's compiled text, or ``fallback``.

        Falls back on any SDK error (including a missing prompt) so the feature
        works against an empty/unconfigured Langfuse project.
        """
        client = self._ensure_client()
        if client is None:
            return fallback
        try:
            prompt = client.get_prompt(name)
        except Exception:  # noqa: BLE001 - absent prompt / SDK error -> fallback
            logger.debug("langfuse get_prompt(%s) failed; using fallback", name)
            return fallback
        # v2 prompts expose .compile()/.prompt; v3 may differ. Be tolerant.
        for attr in ("compile", "get_langchain_prompt"):
            method = getattr(prompt, attr, None)
            if callable(method):
                try:
                    compiled = method()
                    if isinstance(compiled, str):
                        return compiled
                except Exception:  # noqa: BLE001
                    pass
        text = getattr(prompt, "prompt", None)
        if isinstance(text, str):
            return text
        return fallback

    @contextlib.contextmanager
    def trace(self, name: str, **kw):
        """Context manager wrapping an observable unit of work.

        Yields the underlying trace/span object (or ``None`` when unconfigured
        or on any SDK error) so callers can read a trace id if present. Never
        raises out of the ``with`` block setup/teardown.
        """
        client = self._ensure_client()
        if client is None:
            yield None
            return
        span = None
        try:
            start = getattr(client, "trace", None) or getattr(
                client, "start_span", None
            )
            if callable(start):
                try:
                    span = start(name=name, **kw)
                except Exception:  # noqa: BLE001
                    span = None
            yield span
        finally:
            # Best-effort close for SDKs that require it; never raise.
            end = getattr(span, "end", None)
            if callable(end):
                try:
                    end()
                except Exception:  # noqa: BLE001
                    pass

    def flush(self) -> None:
        """Flush pending events, if the SDK supports it. Never raises."""
        client = self._ensure_client()
        if client is None:
            return
        flush = getattr(client, "flush", None)
        if callable(flush):
            try:
                flush()
            except Exception:  # noqa: BLE001
                pass


def get_langfuse_client() -> LangfuseClient:
    """Return a ``LangfuseClient`` built from settings (maybe a no-op).

    Reads only PRESENCE of the three settings; logs presence, never values.
    Constructs no SDK client here (that is lazy on first use).
    """
    secret_key = getattr(settings, "LANGFUSE_SECRET_KEY", "") or ""
    public_key = getattr(settings, "LANGFUSE_PUBLIC_KEY", "") or ""
    base_url = getattr(settings, "LANGFUSE_BASE_URL", "") or ""
    if secret_key and public_key and base_url:
        logger.debug("langfuse configured")
    else:
        logger.debug("langfuse not configured, using local prompt fallback")
    return LangfuseClient(
        secret_key=secret_key,
        public_key=public_key,
        base_url=base_url,
    )
