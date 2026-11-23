"""Entry point for ``langgraph dev`` / LangGraph Studio (see ``langgraph.json``).

Studio loads this module outside Django, so it sets Django up first. The graph
is the global ``chat_agent``'s stored graph JSON (``Agent.graph``), compiled without a
checkpointer (the server brings its own). Tools need a
real ``AgentContext`` (user, session, agent), so use Studio to view and debug
the graph's structure, not to run chat turns.
"""
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "base.settings")
django.setup()

from agents.models import Agent  # noqa: E402
from chat.services.agent.graph import build_graph  # noqa: E402

graph = build_graph(Agent.objects.get(company__isnull=True, slug="chat_agent").graph)
