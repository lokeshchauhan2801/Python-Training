"""seed_agents — idempotently create the built-in tools + a default active
single-agent flow (START -> chat_agent -> END) per company. Run manually, not at
migrate.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agents.models import Agent, Step, Tool
from company.models import Company

# (slug, name, type) — slug == type for every built-in (handler-backed) tool:
# `type` names what the tool does and `slug` selects the handler; they coincide.
_TOOL_SPECS = [
    ("general_assistant", "General assistant", "general_assistant"),
    ("document_search", "Document search", "document_search"),
    ("chat_summarise", "Chat summary", "chat_summarise"),
    ("web_search", "Web search", "web_search"),
    ("add_to_knowledge_base", "Add to knowledge base", "add_to_knowledge_base"),
    (
        "document_processing_status",
        "Document processing status",
        "document_processing_status",
    ),
]

_AGENT_SLUG = "chat_agent"
_STEP_SLUG = "main"


class Command(BaseCommand):
    help = (
        "Idempotently seed the built-in tools and an optional example "
        "single-agent flow (chat_agent) per company."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            dest="company",
            default=None,
            help=(
                "Seed only this company (by id or slug). Defaults to every "
                "company."
            ),
        )

    def handle(self, *args, **options):
        companies = self._resolve_companies(options.get("company"))
        if not companies:
            self.stdout.write(self.style.WARNING("No companies to seed."))
            return
        for company in companies:
            with transaction.atomic():
                self._seed_company(company)

    def _resolve_companies(self, selector):
        if selector is None:
            return list(Company.objects.all())
        company = (
            Company.objects.filter(pk=selector).first()
            or Company.objects.filter(slug=selector).first()
        )
        if company is None:
            raise CommandError(f"No company matches '{selector}'.")
        return [company]

    def _seed_company(self, company):
        tools = {}
        for slug, name, type_ in _TOOL_SPECS:
            tool, created = Tool.objects.get_or_create(
                company=company,
                slug=slug,
                defaults={
                    "name": name,
                    "type": type_,
                    "is_enabled": True,
                },
            )
            tools[slug] = tool
            if created:
                self.stdout.write(f"  + Tool {slug} for {company}")

        agent, created = Agent.objects.get_or_create(
            company=company,
            slug=_AGENT_SLUG,
            defaults={
                "name": "Chat agent",
                "order": 0,
            },
        )
        if created:
            self.stdout.write(f"  + Agent '{_AGENT_SLUG}' for {company}")

        step, step_created = Step.objects.get_or_create(
            agent=agent,
            slug=_STEP_SLUG,
            defaults={
                "company": company,
                "name": "Main step",
                "connected_from_start": True,
                "connected_to_end": True,
                "order": 0,
                "position_x": 0,
                "position_y": 0,
            },
        )
        if step_created:
            self.stdout.write(f"  + Step '{_STEP_SLUG}' for {company}")

        step.tools.add(*tools.values())

        self.stdout.write(self.style.SUCCESS(f"Seeded agents for {company}."))
