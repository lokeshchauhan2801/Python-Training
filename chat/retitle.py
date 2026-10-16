"""Shared first-message session retitle helper.

Extracted verbatim from ``ChatService.process_message`` so both the RAG path and
the LangGraph path call ONE definition. This is a retitle-*or*-touch that ALWAYS
persists the session:

* Default-title case (``session.title == "New session"``): set the title to the
  cleaned ``llm_title`` (strip surrounding quotes/whitespace, truncate to 200)
  when present and non-empty, else ``fallback_text[:50]``; persist
  ``update_fields=["title", "updated_at", "updated_by"]``.
* Non-default case: leave the title untouched but STILL persist
  ``update_fields=["updated_at", "updated_by"]`` (the per-turn audit bump the
  original inline code performed).

The sentinel ``"New session"`` is compared literally.
"""

from __future__ import annotations

_DEFAULT_SESSION_TITLE = "New session"


def retitle_if_default(session, *, llm_title, fallback_text, actor):
    """Retitle (or audit-touch) ``session`` on its first/subsequent message."""
    if session.title == _DEFAULT_SESSION_TITLE:
        cleaned = (llm_title or "").strip().strip("\"'")
        session.title = cleaned[:200] if cleaned else fallback_text[:50]
        session.save(
            actor=actor, update_fields=["title", "updated_at", "updated_by"]
        )
    else:
        session.save(actor=actor, update_fields=["updated_at", "updated_by"])
