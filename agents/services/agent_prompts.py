"""Each Agent's role prompt, kept only in Langfuse.

The prompt says what the agent is for and which tasks it can do. Every step of
the agent's flow answers in that role (``ToolContext.persona``), followed by the
chosen tool's docs. It is named after the agent's slug alone (no folders, no
company: company scoping lives in the database), so agents sharing a slug share
one role. It is created in Langfuse only when an Agent is created
(``agents.signals``: canvas, API, admin, seeds); nothing is stored in the
database.

The prompt's Langfuse ``config`` is the agent's runtime config (model,
temperature, max tokens, retrieval: ``agent_config``). Langfuse versions the
role and config together, so saving one always carries the other forward.

Everything else an agent is given (router prompt and rules, identity,
response format) is code, next to the functions that use it.
"""

from __future__ import annotations


def prompt_name(agent) -> str:
    return agent.slug


def starter_text(agent) -> str:
    """A new agent's prompt until its role is written in Langfuse/the canvas."""
    return f"You are the '{agent.name or agent.slug}' agent."


def _client(langfuse=None):
    from basics.langfuse import get_langfuse_client

    return langfuse or get_langfuse_client()


def get_role_and_config(agent, langfuse=None) -> tuple[str, dict]:
    """``(role, config)`` from one fetch; the config is resolved (defaulted)."""
    from agents.services import agent_config

    text, config = _client(langfuse).get_prompt_and_config(
        prompt_name(agent), fallback=""
    )
    return text.strip(), agent_config.resolve(config)


def save(agent, *, text: str | None = None, config: dict | None = None, langfuse=None) -> bool:
    """Store a new production version with ``text`` and/or ``config``.

    Whatever is not given is copied from the latest version (read uncached),
    so saving the role keeps the config and saving the config keeps the role.
    ``config`` must already be validated (``agent_config.validate``); keys
    added in Langfuse that this code does not know are kept.
    """
    from agents.services import agent_config

    client = _client(langfuse)
    latest = client.latest_prompt(prompt_name(agent))
    if latest is None:
        # Langfuse could not be read: writing now could replace the other
        # part with a default, so save nothing.
        return False
    current_text, current_config = latest
    new_text = (text if text is not None else current_text).strip() or starter_text(agent)
    if config is not None:
        new_config = {**current_config, **config}
    else:
        new_config = current_config or agent_config.defaults()
    changed = [part for part, given in (("role", text), ("config", config)) if given is not None]
    saved = client.create_prompt(
        prompt_name(agent),
        new_text,
        config=new_config,
        commit_message=f"canvas: {' + '.join(changed) or 'save'}",
    )
    if saved:
        client.flush()
    return saved


def ensure_role(agent, text: str | None = None, langfuse=None, config: dict | None = None) -> bool:
    """Create the agent's prompt when it does not exist yet (never overwrites).

    A new prompt starts with ``config`` or the default config.
    """
    from agents.services import agent_config

    client = _client(langfuse)
    created = client.ensure_prompt(
        prompt_name(agent),
        text or starter_text(agent),
        config=config or agent_config.defaults(),
    )
    if created:
        client.flush()
    return created
