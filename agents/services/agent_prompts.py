"""Each Agent's role prompt, kept only in Langfuse.

The prompt says what the agent is for and which tasks it can do. Every step of
the agent's flow answers in that role (``ToolContext.persona``), followed by the
chosen tool's docs. It is named after the agent's slug alone (no folders, no
company: company scoping lives in the database), so agents sharing a slug share
one role. It is created in Langfuse only when an Agent is created
(``agents.signals``: canvas, API, admin, seeds); nothing is stored in the
database.
"""

from __future__ import annotations

# Langfuse prompts the app reads that are not agent roles.
RESERVED_PROMPT_NAMES = {"agent_router"}


def prompt_name(agent) -> str:
    return agent.slug


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
