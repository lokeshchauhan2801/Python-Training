"""seed_multi_agent_demo — idempotently create a multi-agent demo flow.

Creates, per company (only what is missing):

* ``research-agent``  — one worker that finds information (documents, web,
  general knowledge).
* ``writer-agent``    — one worker whose tools work on other parts' answers
  (report, action items, email, translation, chart, chat summary).
* ``multi-agent-assistant`` — a supervisor with three workers: Research
  (hands off to research-agent), Files (its own add-to-knowledge-base / status
  tools) and Write up (hands off to writer-agent).

Select "Multi-agent assistant" in chat to try requests such as "find our leave
policy and draft an email about it to HR".
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agents.models import Agent, Step, Tool
from company.models import Company

_TOOL_NAMES = {
    "general_assistant": "General assistant",
    "document_search": "Document search",
    "web_search": "Web search",
    "chat_summarise": "Chat summary",
    "add_to_knowledge_base": "Add to knowledge base",
    "document_processing_status": "Document processing status",
    "report_writer": "Write report",
    "action_items": "Extract action items",
    "email_drafter": "Draft email",
    "translator": "Translate",
    "chart_builder": "Chart from results",
}

# (slug, name, description, [(step slug, step name, tool slugs, delegate slug)])
_AGENTS = [
    (
        "research-agent",
        "Research agent",
        "Finds information: searches the company documents and attached files, "
        "the web, or answers from general knowledge.",
        [("find", "Find information", ["document_search", "web_search", "general_assistant"], None)],
    ),
    (
        "writer-agent",
        "Writer agent",
        "Writes things up from answers found by other agents: reports, action "
        "items, emails, translations and charts, plus chat summaries.",
        [
            (
                "write",
                "Write it up",
                [
                    "report_writer",
                    "action_items",
                    "email_drafter",
                    "translator",
                    "chart_builder",
                    "chat_summarise",
                ],
                None,
            )
        ],
    ),
    (
        "multi-agent-assistant",
        "Multi-agent assistant",
        "A supervisor that splits each request between the Research agent, its "
        "own Files worker and the Writer agent.",
        [
            ("research", "Research", [], "research-agent"),
            ("files", "Files", ["add_to_knowledge_base", "document_processing_status"], None),
            ("write-up", "Write up", [], "writer-agent"),
        ],
    ),
]


class Command(BaseCommand):
    help = "Idempotently create the multi-agent demo flow (research + writer agents)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            dest="company",
            default=None,
            help="Seed only this company (by id, slug or name). Defaults to every company.",
        )

    def handle(self, *args, **options):
        companies = self._resolve_companies(options.get("company"))
        for company in companies:
            with transaction.atomic():
                self._seed_company(company)

    def _resolve_companies(self, selector):
        if selector is None:
            return list(Company.objects.all())
        company = (
            Company.objects.filter(pk=selector).first()
            or Company.objects.filter(slug=selector).first()
            or Company.objects.filter(name=selector).first()
        )
        if company is None:
            raise CommandError(f"No company matches '{selector}'.")
        return [company]

    def _seed_company(self, company):
        tools = {}
        for slug, name in _TOOL_NAMES.items():
            tools[slug], _ = Tool.objects.get_or_create(
                company=company,
                slug=slug,
                defaults={"name": name, "type": slug, "is_enabled": True},
            )

        agents = {}
        for agent_slug, agent_name, description, steps in _AGENTS:
            agent, created = Agent.objects.get_or_create(
                company=company,
                slug=agent_slug,
                defaults={"name": agent_name, "description": description},
            )
            agents[agent_slug] = agent
            if not created and not agent.description:
                agent.description = description
                agent.save(update_fields=["description", "updated_at"])
            if not created or Step.objects.filter(agent=agent).exists():
                self.stdout.write(f"  = Agent '{agent_slug}' already exists for {company}")
                continue

            for index, (step_slug, step_name, tool_slugs, delegate) in enumerate(steps):
                step = Step.objects.create(
                    company=company,
                    agent=agent,
                    slug=step_slug,
                    name=step_name,
                    delegate_agent=agents.get(delegate) if delegate else None,
                    order=index,
                    position_x=0,
                    position_y=index * 170,
                )
                step.tools.add(*[tools[s] for s in tool_slugs])
            self.stdout.write(f"  + Agent '{agent_slug}' for {company}")

        self.stdout.write(self.style.SUCCESS(f"Seeded the multi-agent demo for {company}."))
