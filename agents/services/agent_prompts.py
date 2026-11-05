"""Each Agent's role prompt, kept only in Langfuse.

The prompt says what the agent is for and which tasks it can do. Every worker
of the agent answers in that role (``_persona``) and the supervisor routes work
by it (``Worker.describe``). It is named ``agents/<company slug>/<agent slug>``
(agent slugs repeat across companies). It is created in Langfuse only when an
Agent is created (``agents.signals``: canvas, API, admin, seeds); nothing is
stored in the database.
"""

from __future__ import annotations


def prompt_name(agent) -> str:
    company = agent.company.slug if agent.company_id else "global"
    return f"agents/{company}/{agent.slug}"


def starter_text(agent) -> str:
    """A new agent's prompt until its role is written in Langfuse/the canvas."""
    return f"You are the '{agent.name or agent.slug}' agent."


def _client(langfuse=None):
    from basics.langfuse import get_langfuse_client

    return langfuse or get_langfuse_client()


def get_role(agent, langfuse=None) -> str:
    """The agent's role prompt ("" when Langfuse cannot serve it)."""
    return _client(langfuse).get_prompt(prompt_name(agent), fallback="").strip()


def save_role(agent, text: str, langfuse=None) -> bool:
    """Store ``text`` as the agent's new production prompt version."""
    client = _client(langfuse)
    saved = client.create_prompt(prompt_name(agent), text.strip())
    if saved:
        client.flush()
    return saved


def ensure_role(agent, text: str | None = None, langfuse=None) -> bool:
    """Create the agent's prompt when it does not exist yet (never overwrites)."""
    client = _client(langfuse)
    created = client.ensure_prompt(prompt_name(agent), text or starter_text(agent))
    if created:
        client.flush()
    return created
