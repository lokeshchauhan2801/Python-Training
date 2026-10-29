"""Agents API viewsets backing the canvas (owner/company-scoped, 404 out-of-scope)."""

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import Agent, Step, Tool
from .serializers import AgentSerializer, StepSerializer, ToolSerializer

# Human-readable descriptions for the registered handler functions. The runtime
# registry (chat/services/llm_tools.py) maps a slug -> handler but carries no
# description, so the canvas's "add tool from function" list sources its labels
# here. A slug with no entry falls back to a titleised slug.
_FUNCTION_DESCRIPTIONS = {
    "general_assistant": "Plain LLM answer with no retrieval (chart-capable).",
    "document_search": "Answer from the company's uploaded documents (RAG, chart-capable).",
    "chat_summarise": "Summarise the current conversation.",
    "web_search": "Answer from the model's general knowledge (no live web).",
    "add_to_knowledge_base": "Add an attached file to the knowledge base (background processing).",
    "document_processing_status": "Report the status of documents being processed.",
}

# Slugs that exist in the registry but are internal plumbing, not user-facing
# tools to attach to an agent from the canvas.
_HIDDEN_FUNCTION_SLUGS = {"prompt_tool"}


def _function_catalog():
    """The attachable handler functions: (slug, name, type, description).

    Sourced from the live registry (``available_slugs``) minus internal slugs.
    ``type`` mirrors the slug (the Tool.type choices are the handler slugs), so
    a created Tool row is valid under Tool.clean()/the serializer.
    """
    from chat.services.langgraph_service import available_slugs

    catalog = []
    for slug in sorted(available_slugs()):
        if slug in _HIDDEN_FUNCTION_SLUGS:
            continue
        catalog.append(
            {
                "slug": slug,
                "name": slug.replace("_", " ").title(),
                "type": slug,
                "description": _FUNCTION_DESCRIPTIONS.get(slug, ""),
            }
        )
    return catalog


def _entry_step(agent):
    """The entry step of ``agent`` (connected_from_start, deterministic)."""
    return (
        Step.objects.filter(agent=agent, connected_from_start=True)
        .order_by("order", "created_at")
        .first()
    )


def _walk_steps(agent):
    """Forward-walk the ``next_step`` chain from the agent's entry step.

    Mirrors ``LangGraphChatService._walk_steps``: follows ``next_step_id`` via
    the default manager (so soft-deleted hops terminate the walk), guards cycles
    with a ``seen`` set and a hop cap, scoped to one agent. Returns the ordered
    list of Step rows (``[]`` when the agent has no entry step).
    """
    entry = _entry_step(agent)
    chain = []
    if entry is None:
        return chain
    seen = set()
    current = entry
    hops = 0
    cap = Step.objects.filter(agent=agent).count() * 2 + 1
    while current is not None and current.pk not in seen and hops <= cap:
        chain.append(current)
        seen.add(current.pk)
        hops += 1
        if current.connected_to_end:
            break
        next_id = current.next_step_id
        current = Step.objects.filter(pk=next_id).first() if next_id else None
    return chain


class ToolViewSet(ScopedModelViewSet):
    queryset = Tool.objects.all()
    serializer_class = ToolSerializer

    def _resolve_company(self, request):
        """Resolve the target company the same way create/load do.

        An explicit ``company`` (body or ``?company=``) is validated against
        ownership via the serializer; otherwise the user's single owned company
        is used. Reuses ``AgentSerializer._resolve_company`` (the sync_langfuse
        idiom) so a multi-company user is handled, not guessed.
        """
        context = self.get_serializer_context()
        serializer = ToolSerializer(context=context)
        supplied_id = request.data.get("company") or request.query_params.get("company")
        supplied = None
        if supplied_id:
            from company.models import Company

            supplied = Company.objects.filter(pk=supplied_id).first()
        return serializer._resolve_company(supplied)

    @action(detail=False, methods=["get"])
    def available(self, request):
        """List the registered handler FUNCTIONS the canvas can attach as tools.

        Each entry marks whether a backing Tool row already exists for the
        resolved company (``tool_id``) so the UI can show "added" vs "add".
        """
        company = self._resolve_company(request)
        existing = {
            t.slug: str(t.pk)
            for t in Tool.objects.filter(company=company)
        }
        catalog = []
        for fn in _function_catalog():
            catalog.append({**fn, "tool_id": existing.get(fn["slug"])})
        return Response(catalog, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"])
    def attach(self, request):
        """Create a Tool row from a function slug (idempotent) and optionally
        attach it to a STEP.

        Body: ``{ "slug": <function slug>, "step": <step id, optional>,
        "company": <id, optional> }``. The Tool row is created once per
        (company, slug); re-attaching reuses it. Returns the Tool.
        """
        company = self._resolve_company(request)
        slug = (request.data.get("slug") or "").strip()
        if not slug:
            return Response(
                {"detail": "A function 'slug' is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        catalog = {fn["slug"]: fn for fn in _function_catalog()}
        fn = catalog.get(slug)
        if fn is None:
            return Response(
                {"detail": f"'{slug}' is not an attachable handler function."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            tool, _created = Tool.objects.get_or_create(
                company=company,
                slug=slug,
                defaults={
                    "name": fn["name"],
                    "type": fn["type"],
                    "description": fn["description"],
                    "is_enabled": True,
                    "created_by": request.user,
                    "updated_by": request.user,
                },
            )

            step_id = request.data.get("step")
            if step_id:
                step = Step.objects.filter(pk=step_id, company=company).first()
                if step is None:
                    return Response(
                        {"detail": "Step not found or not in this company."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                step.tools.add(tool)

        context = self.get_serializer_context()
        return Response(
            ToolSerializer(tool, context=context).data,
            status=status.HTTP_200_OK,
        )


class AgentViewSet(ScopedModelViewSet):
    queryset = Agent.objects.all()
    serializer_class = AgentSerializer

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Make the target agent the single active flow for its company.

        ``is_active`` lives on the Agent container, so activation targets the
        agent directly (no entry resolution). Clears any other active agent for
        the company and marks the target active inside one transaction. A
        half-built agent (no entry step) MAY be activated; it degrades to RAG at
        runtime via the orchestrator gate.
        """
        target = self.get_object()
        with transaction.atomic():
            Agent.objects.filter(company=target.company).exclude(
                pk=target.pk
            ).update(is_active=False)
            target.is_active = True
            target.save(
                actor=request.user,
                update_fields=["is_active", "updated_at", "updated_by"],
            )
        return Response(
            self.get_serializer(target).data, status=status.HTTP_200_OK
        )

    @action(detail=False, methods=["get"])
    def load(self, request):
        """Return the canvas payload: all the company's agents + a selected
        agent's steps + the company tools.

        - ``agents`` = every agent for the company (dropdown).
        - ``selected_agent_id`` = the ``?agent=`` selector if in scope, else the
          active agent, else the first agent, else null (NEVER 404 on a stale or
          foreign id).
        - ``steps`` = ALL of the selected agent's steps (incl. orphans, so a
          freshly created step is visible immediately).
        - ``active_agent_id`` = the company's active Agent (for the dropdown
          marker / Activate button).
        """
        context = self.get_serializer_context()
        serializer = AgentSerializer(context=context)

        supplied_id = request.query_params.get("company") or request.data.get(
            "company"
        )
        supplied = None
        if supplied_id:
            from company.models import Company

            supplied = Company.objects.filter(pk=supplied_id).first()
        company = serializer._resolve_company(supplied)

        tools_qs = Tool.objects.filter(company=company).order_by("slug")
        tools_data = ToolSerializer(tools_qs, many=True, context=context).data

        all_agents = list(
            Agent.objects.filter(company=company).order_by("order", "created_at")
        )
        active = next((a for a in all_agents if a.is_active), None)

        # Resolve the selected agent, falling back silently (never 404).
        agent_id = request.query_params.get("agent") or request.data.get("agent")
        selected = None
        if agent_id:
            selected = self.get_queryset().filter(pk=agent_id).first()
        if selected is None:
            selected = active or (all_agents[0] if all_agents else None)

        steps_data = []
        if selected is not None:
            steps_qs = Step.objects.filter(agent=selected).order_by(
                "order", "created_at"
            )
            steps_data = StepSerializer(
                steps_qs, many=True, context=context
            ).data

        return Response(
            {
                "active_agent_id": str(active.pk) if active else None,
                "agents": AgentSerializer(
                    all_agents, many=True, context=context
                ).data,
                "selected_agent_id": str(selected.pk) if selected else None,
                "steps": steps_data,
                "tools": tools_data,
            },
            status=status.HTTP_200_OK,
        )


class StepViewSet(ScopedModelViewSet):
    queryset = Step.objects.all()
    serializer_class = StepSerializer
