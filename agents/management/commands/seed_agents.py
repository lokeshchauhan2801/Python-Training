"""seed_agents — idempotently create the global built-in tools and the global
``chat_agent`` (START -> main -> END), usable by every company. Run manually,
not at migrate (migration 0015 does the same once).

The agent's role is its Langfuse prompt ``chat_agent``; saving the Agent
creates it there when missing (``agents.signals``).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from agents.models import Agent, Step, Tool
from agents.services.tool_docs import DEFAULT_TOOL_DOCS

# (slug, name) of every built-in tool; the slug selects the LangChain tool
# (``general_assistant`` binds none: the step's model answers directly).
_TOOL_SPECS = [
    ("general_assistant", "General assistant"),
    ("document_search", "Document search"),
    ("create_chart", "Create chart"),
    ("chart_from_file", "Chart from file"),
    ("chat_summarise", "Chat summary"),
    ("web_search", "Web search"),
    ("add_to_knowledge_base", "Add to knowledge base"),
    ("document_processing_status", "Document processing status"),
]

_AGENT_SLUG = "chat_agent"
_STEP_SLUG = "main"


class Command(BaseCommand):
    help = "Idempotently seed the global built-in tools and the global chat_agent."

    @transaction.atomic
    def handle(self, *args, **options):
        tools = []
        for slug, name in _TOOL_SPECS:
            tool = Tool.objects.filter(company__isnull=True, slug=slug).first()
            if tool is None:
                tool = Tool.objects.create(
                    company=None,
                    slug=slug,
                    name=name,
                    description=DEFAULT_TOOL_DOCS.get(slug, ""),
                    is_enabled=True,
                )
                self.stdout.write(f"  + global tool {slug}")
            tools.append(tool)

        agent = Agent.objects.filter(company__isnull=True, slug=_AGENT_SLUG).first()
        if agent is None:
            agent = Agent.objects.create(company=None, slug=_AGENT_SLUG, name="Chat agent")
            self.stdout.write(f"  + global agent '{_AGENT_SLUG}'")

        step, created = Step.objects.get_or_create(
            agent=agent,
            slug=_STEP_SLUG,
            defaults={
                "company": None,
                "name": "Main step",
                "connected_from_start": True,
                "connected_to_end": True,
                "order": 0,
            },
        )
        if created:
            step.tools.set(tools)
            self.stdout.write(f"  + step '{_STEP_SLUG}'")

        self.stdout.write(self.style.SUCCESS("Seeded the global chat agent."))
