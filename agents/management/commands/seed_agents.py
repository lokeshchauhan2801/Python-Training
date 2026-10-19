"""seed_agents — idempotently create the built-in tools + a default (inactive)
START -> chat_agent -> END graph per company. Run manually, not at migrate.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agents.models import AgentEdge, AgentGraph, AgentNode, Tool
from company.models import Company

# (slug, display name, is_enabled) for the four built-in tools.
_TOOL_SPECS = [
    ("general_assistant", "General assistant", True),
    ("document_search", "Document search", True),
    ("chat_summarise", "Chat summary", True),
    ("web_search", "Web search", True),
]

_DEFAULT_GRAPH_NAME = "Default agent"
_STEP_SLUG = "chat_agent"


class Command(BaseCommand):
    help = (
        "Idempotently seed the four built-in tools and a default inactive "
        "START -> chat_agent -> END graph per company."
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
        for slug, name, is_enabled in _TOOL_SPECS:
            tool, created = Tool.objects.get_or_create(
                company=company,
                slug=slug,
                defaults={"name": name, "is_enabled": is_enabled},
            )
            tools[slug] = tool
            if created:
                self.stdout.write(f"  + Tool {slug} for {company}")

        graph, created = AgentGraph.objects.get_or_create(
            company=company,
            name=_DEFAULT_GRAPH_NAME,
            defaults={"is_active": False},
        )
        if created:
            self.stdout.write(f"  + Graph '{_DEFAULT_GRAPH_NAME}' for {company}")

        start, _ = AgentNode.objects.get_or_create(
            graph=graph,
            slug="start",
            defaults={"company": company, "kind": AgentNode.KIND_START},
        )
        step, _ = AgentNode.objects.get_or_create(
            graph=graph,
            slug=_STEP_SLUG,
            defaults={
                "company": company,
                "kind": AgentNode.KIND_STEP,
                "label": "Chat agent",
            },
        )
        end, _ = AgentNode.objects.get_or_create(
            graph=graph,
            slug="end",
            defaults={"company": company, "kind": AgentNode.KIND_END},
        )

        # All four tools attached to the STEP router node (M2M add is idempotent).
        step.tools.add(*tools.values())

        for src, tgt in ((start, step), (step, end)):
            AgentEdge.objects.get_or_create(
                source=src,
                target=tgt,
                defaults={"company": company, "graph": graph},
            )

        self.stdout.write(self.style.SUCCESS(f"Seeded agents for {company}."))
