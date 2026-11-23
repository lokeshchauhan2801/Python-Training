"""Pick which service answers a chat message.

The send URL names the agent by its route (``Agent.route``, saved per agent):

* ``POST /chat/<session>/send/``         -> no agent: ``ChatService`` (normal RAG)
* ``POST /chat/<session>/send/<route>/`` -> the Agent with that route:
  ``AgentChatService`` (its LangGraph graph from ``Agent.graph``)

``ChatService`` (``chat_service.py``) is the one chat service. The agent
runtime (``agent/``) sits on top of it and uses it to save turns and for RAG.
"""
from __future__ import annotations

import logging

from django.db.models import Q

from .chat_service import ChatProviderError, ChatService

logger = logging.getLogger(__name__)

NORMAL_RAG = "normal-rag"


def find_agent(company, route):
    """The Agent whose ``route`` is ``route`` for ``company``, or ``None``.

    A company's own Agent wins over a global one (no company) with the same route.
    """
    from agents.models import Agent

    if not route:
        return None
    return (
        Agent.objects.filter(Q(company=company) | Q(company__isnull=True), route=route)
        .order_by("company_id", "created_at")  # NULLs (global) sort last
        .first()
    )


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
        logger.exception("Could not build the runtime for agent %s", agent.route)
        raise ChatProviderError("The selected agent runtime could not be constructed.") from exc
