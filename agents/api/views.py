"""Agents API viewsets backing the canvas (owner/company-scoped, 404 out-of-scope)."""

from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import Agent, Step, Tool
from ..services import agent_config
from ..services.tool_docs import DEFAULT_TOOL_DOCS
from .serializers import AgentSerializer, StepSerializer, ToolSerializer

# Slugs that exist in the registry but are internal plumbing, not user-facing
# tools to attach to an agent from the canvas.
_HIDDEN_FUNCTION_SLUGS = {"prompt_tool"}


def _function_catalog():
    """The attachable handler functions: (slug, name, description).

    Sourced from the live registry (``available_slugs``) minus internal slugs;
    ``description`` is the tool's default docs (``DEFAULT_TOOL_DOCS``).
    """
    from chat.services.agent.tools import available_slugs

    catalog = []
    for slug in sorted(available_slugs()):
        if slug in _HIDDEN_FUNCTION_SLUGS:
            continue
        catalog.append(
            {
                "slug": slug,
                "name": slug.replace("_", " ").title(),
                "description": DEFAULT_TOOL_DOCS.get(slug, ""),
            }
        )
    return catalog


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

    def _tool_company(self, request):
        """The company whose tools apply: the step's/agent's when one is given
        (``None`` for a global agent), else the resolved company."""
        step_id = request.data.get("step") or request.query_params.get("step")
        agent_id = request.data.get("agent") or request.query_params.get("agent")
        owner = None
        if step_id:
            step = Step.objects.filter(pk=step_id).select_related("agent").first()
            owner = step.agent if step else None
        elif agent_id:
            owner = Agent.objects.filter(pk=agent_id).first()
        if owner is not None and owner.company_id is None:
            if not request.user.is_superuser:
                from rest_framework.exceptions import PermissionDenied

                raise PermissionDenied("only an administrator can edit a global agent")
            return None
        return self._resolve_company(request)

    @action(detail=False, methods=["get"])
    def available(self, request):
        """List the registered handler FUNCTIONS the canvas can attach as tools.

        Each entry marks whether a backing Tool row already exists for the
        resolved company (``tool_id``) so the UI can show "added" vs "add".
        """
        company = self._tool_company(request)
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
        company = self._tool_company(request)
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
                    "description": fn["description"],
                    "is_enabled": True,
                    "created_by": request.user,
                    "updated_by": request.user,
                },
            )

            step_id = request.data.get("step")
            if step_id:
                step = Step.objects.filter(
                    pk=step_id, company=company, agent__company=company
                ).first()
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

    @action(detail=False, methods=["get"])
    def load(self, request):
        """Return the canvas payload: all the company's agents + a selected
        agent's steps + the company tools.

        - ``agents`` = every agent for the company (dropdown).
        - ``selected_agent_id`` = the ``?agent=`` selector if in scope, else the
          oldest agent (``created_at``), else null (NEVER 404 on
          a stale or foreign id).
        - ``steps`` = ALL of the selected agent's steps (incl. orphans, so a
          freshly created step is visible immediately).

        There is no activation: multiple live Agents coexist and none is
        implicitly active. The builder selects an Agent to edit without
        activating it, so the payload carries no ``active_agent_id``.
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

        # The company's agents, plus the global ones for an administrator
        # (only a superuser may edit a global agent).
        scope = Q(company=company)
        if request.user.is_superuser:
            scope |= Q(company__isnull=True)
        visible = Agent.objects.filter(scope)
        all_agents = list(visible.order_by("created_at"))

        # Resolve the selected agent, falling back silently (never 404).
        agent_id = request.query_params.get("agent") or request.data.get("agent")
        selected = None
        if agent_id:
            selected = visible.filter(pk=agent_id).first()
        if selected is None:
            selected = all_agents[0] if all_agents else None

        # A global agent's steps use the global tools.
        tools_qs = Tool.objects.filter(
            company=selected.company if selected is not None else company
        ).order_by("slug")
        tools_data = ToolSerializer(tools_qs, many=True, context=context).data

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
                "agents": AgentSerializer(
                    all_agents, many=True, context=context
                ).data,
                "selected_agent_id": str(selected.pk) if selected else None,
                "steps": steps_data,
                "tools": tools_data,
                # The stored graph JSON chat runs (``Agent.graph``); the
                # canvas does not read it.
                "graph": selected.graph if selected is not None else None,
                # Prefill for a new agent's config and the allowed values.
                "config_defaults": agent_config.defaults(),
                "config_options": {
                    "models": agent_config.model_options(),
                    "limits": agent_config.limits(),
                },
            },
            status=status.HTTP_200_OK,
        )


class StepViewSet(ScopedModelViewSet):
    queryset = Step.objects.all()
    serializer_class = StepSerializer
