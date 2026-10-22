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
        self._client = None

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
                import langfuse

                self._client = langfuse.Langfuse(
                    public_key=self._public_key,
                    secret_key=self._secret_key,
                    host=self._base_url,
                )
            except Exception:
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
        except Exception:
            logger.debug("langfuse get_prompt(%s) failed; using fallback", name)
            return fallback

        for attr in ("compile", "get_langchain_prompt"):
            method = getattr(prompt, attr, None)
            if callable(method):
                try:
                    compiled = method()
                    if isinstance(compiled, str):
                        return compiled
                except Exception:
                    pass
        text = getattr(prompt, "prompt", None)
        if isinstance(text, str):
            return text
        return fallback

    def create_prompt(self, name: str, text: str, *, labels=None) -> bool:
        """Create/version a TEXT prompt in Langfuse. Returns True on success.

        A no-op returning False when Langfuse is unconfigured or the SDK errors
        (so a push command degrades gracefully offline). Labels default to
        ["production"] so the created version is the one get_prompt() serves.
        """
        client = self._ensure_client()
        if client is None:
            return False
        create = getattr(client, "create_prompt", None)
        if not callable(create):
            return False
        try:
            create(
                name=name,
                prompt=text,
                type="text",
                labels=list(labels) if labels is not None else ["production"],
            )
            return True
        except Exception:
            logger.debug("langfuse create_prompt(%s) failed", name)
            return False

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
                except Exception:
                    span = None
            yield span
        finally:

            end = getattr(span, "end", None)
            if callable(end):
                try:
                    end()
                except Exception:
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
            except Exception:
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
