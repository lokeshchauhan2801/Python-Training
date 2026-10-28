"""Agents API viewsets backing the canvas (owner/company-scoped, 404 out-of-scope)."""

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import Agent, Tool
from .serializers import AgentSerializer, ToolSerializer


def _walk_chain(entry):
    """Forward-walk the ``next_node`` chain from ``entry``.

    Mirrors ``LangGraphChatService._walk_chain``: follows ``next_node_id`` via
    the default manager (so soft-deleted hops terminate the walk), guards cycles
    with a ``seen`` set and a hop cap. Returns the ordered list of Agent rows.
    """
    chain = []
    seen = set()
    current = entry
    hops = 0
    company_id = getattr(entry, "company_id", None)
    cap = (
        Agent.objects.filter(company_id=company_id).count() * 2 + 1
        if company_id is not None
        else Agent.objects.count() * 2 + 1
    )
    while current is not None and current.pk not in seen and hops <= cap:
        chain.append(current)
        seen.add(current.pk)
        hops += 1
        if current.connected_to_end:
            break
        next_id = current.next_node_id
        current = Agent.objects.filter(pk=next_id).first() if next_id else None
    return chain


class ToolViewSet(ScopedModelViewSet):
    queryset = Tool.objects.all()
    serializer_class = ToolSerializer


class AgentViewSet(ScopedModelViewSet):
    queryset = Agent.objects.all()
    serializer_class = AgentSerializer

    def _resolve_entry_for(self, target):
        """Resolve the flow entry (forward-only) whose chain contains ``target``.

        If the target is itself an entry (``connected_from_start``), it is the
        entry. Otherwise iterate the company's candidate entries ordered by
        ``order, created_at`` and return the first whose forward chain contains
        the target's pk. Returns ``None`` when no entry reaches the target.
        """
        if target.connected_from_start:
            return target
        candidates = Agent.objects.filter(
            company=target.company, connected_from_start=True
        ).order_by("order", "created_at")
        for candidate in candidates:
            chain = _walk_chain(candidate)
            if any(a.pk == target.pk for a in chain):
                return candidate
        return None

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Make the target's flow the single active flow for its company.

        Resolves the flow entry forward-only, then clears any other active agent
        for the company and marks the entry active inside one transaction.
        """
        target = self.get_object()
        entry = self._resolve_entry_for(target)
        if entry is None:
            return Response(
                {
                    "detail": (
                        "This agent is not part of a flow with a start node; "
                        "cannot activate."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            Agent.objects.filter(company=entry.company).exclude(
                pk=entry.pk
            ).update(is_active=False)
            entry.is_active = True
            entry.save(
                actor=request.user,
                update_fields=["is_active", "updated_at", "updated_by"],
            )
        return Response(
            self.get_serializer(entry).data, status=status.HTTP_200_OK
        )

    @action(detail=False, methods=["get"])
    def load(self, request):
        """Return the canvas payload for the company's active (or first) flow."""
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

        entry = (
            Agent.objects.filter(
                company=company, connected_from_start=True, is_active=True
            )
            .order_by("order", "created_at")
            .first()
        )
        if entry is None:
            entry = (
                Agent.objects.filter(company=company, connected_from_start=True)
                .order_by("order", "created_at")
                .first()
            )

        if entry is None:
            return Response(
                {
                    "entry_id": None,
                    "is_active": False,
                    "agents": [],
                    "tools": tools_data,
                },
                status=status.HTTP_200_OK,
            )

        chain = _walk_chain(entry)
        return Response(
            {
                "entry_id": str(entry.pk),
                "is_active": bool(entry.is_active),
                "agents": AgentSerializer(chain, many=True, context=context).data,
                "tools": tools_data,
            },
            status=status.HTTP_200_OK,
        )
