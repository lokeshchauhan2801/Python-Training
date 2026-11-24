"""Pick which service answers a chat message.

Each agent's slug is its complete chat route (``Agent.slug``, e.g.
``/chat/chat_agent/``, unique across all agents). The request path is matched
against it:

* ``POST /chat/``            -> no agent: ``ChatService`` (normal RAG)
* ``POST /chat/chat_agent/`` -> the Agent whose slug is ``/chat/chat_agent/``:
  ``AgentChatService`` (its LangGraph graph from ``Agent.graph``)

The chat session is the ``session_id`` in the request body.

``ChatService`` (``chat_service.py``) is the one chat service. The agent
runtime (``agent/``) sits on top of it and uses it to save turns and for RAG.
"""
from __future__ import annotations

import logging

from django.db.models import Q

from .chat_service import ChatProviderError, ChatService

logger = logging.getLogger(__name__)

NORMAL_RAG = "normal-rag"


def find_agent(company, slug):
    """The Agent whose slug (complete route, e.g. ``/chat/chat_agent/``) is
    ``slug`` and that ``company`` may use, or ``None``.

    Slugs are unique across all agents, so a slug names one agent; it must be
    ``company``'s own or a global one (no company).
    """
    from agents.models import Agent

    if not slug:
        return None
    return Agent.objects.filter(
        Q(company=company) | Q(company__isnull=True), slug=slug
    ).first()


def chat_service(llm_client=None) -> ChatService:
    """The normal RAG service."""
    return ChatService(llm_client=llm_client) if llm_client is not None else ChatService()


def service_for(agent=None, llm_client=None):
    """``ChatService`` with no agent, else an ``AgentChatService`` for ``agent``.

    A failure building the agent runtime is raised as ``ChatProviderError`` so
    the view answers 502.
    """
    if agent is None:
        return chat_service(llm_client)
    try:
        from basics.langfuse import get_langfuse_client

        from .agent.service import AgentChatService

        return AgentChatService(
            chat_service=chat_service(llm_client),
            langfuse=get_langfuse_client(),
            agent=agent,
        )
    except Exception as exc:
        logger.exception("Could not build the runtime for agent %s", agent.slug)
        raise ChatProviderError("The selected agent runtime could not be constructed.") from exc
