"""Agents API viewsets backing the canvas (owner/company-scoped, 404 out-of-scope)."""

from django.db import transaction
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from basics.api.views import ScopedModelViewSet

from ..models import AgentEdge, AgentGraph, AgentNode, Tool
from .serializers import (
    AgentEdgeSerializer,
    AgentGraphSerializer,
    AgentNodeSerializer,
    ToolSerializer,
)


class AgentGraphViewSet(ScopedModelViewSet):
    queryset = AgentGraph.objects.all()
    serializer_class = AgentGraphSerializer

    @action(detail=True, methods=["get"])
    def load(self, request, pk=None):
        # GET /graph/{id}/load/ — the whole canvas payload in one response.
        """Return the aggregated canvas payload for this graph."""
        graph = self.get_object()
        nodes = graph.nodes.order_by("created_at")
        edges = graph.edges.select_related("source", "target").order_by(
            "source__slug", "target__slug"
        )
        tools_qs = Tool.objects.filter(company=graph.company).order_by("slug")
        context = self.get_serializer_context()
        payload = {
            "graph": AgentGraphSerializer(graph, context=context).data,
            "nodes": AgentNodeSerializer(nodes, many=True, context=context).data,
            "edges": AgentEdgeSerializer(edges, many=True, context=context).data,
            "tools": ToolSerializer(tools_qs, many=True, context=context).data,
        }
        return Response(payload, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Make this graph the single active graph for its company.

        Clear-then-set order inside a transaction avoids transiently having two
        active graphs (which the partial unique constraint would reject).
        """
        graph = self.get_object()
        with transaction.atomic():
            AgentGraph.objects.filter(
                company=graph.company, is_active=True
            ).exclude(pk=graph.pk).update(is_active=False)
            graph.is_active = True
            graph.save(actor=request.user, update_fields=["is_active", "updated_at", "updated_by"])
        return Response(
            self.get_serializer(graph).data, status=status.HTTP_200_OK
        )


class AgentNodeViewSet(ScopedModelViewSet):
    queryset = AgentNode.objects.all()
    serializer_class = AgentNodeSerializer


class AgentEdgeViewSet(ScopedModelViewSet):
    queryset = AgentEdge.objects.all()
    serializer_class = AgentEdgeSerializer


class ToolViewSet(ScopedModelViewSet):
    queryset = Tool.objects.all()
    serializer_class = ToolSerializer

    @action(detail=False, methods=["post"])
    def sync_langfuse(self, request):
        """Upsert prompt-only Tools from client-posted Langfuse prompt names.

        langfuse 4.18.0 has NO prompt-listing API, so the posted-names path is
        the only viable one: the canvas sends the prompt names it knows about.
        With no names this is a graceful no-op returning the company's current
        tools. Each name becomes an idempotent prompt-only Tool (existing rows
        are left untouched).
        """
        context = self.get_serializer_context()
        company = ToolSerializer(context=context)._resolve_company(None)

        names = request.data.get("prompt_names") or []
        if isinstance(names, str):
            names = [names]

        with transaction.atomic():
            for raw in names:
                name = (raw or "").strip()
                if not name:
                    continue
                slug = slugify(name)[:64]
                if not slug:
                    continue
                Tool.objects.get_or_create(
                    company=company,
                    slug=slug,
                    defaults={
                        "name": name,
                        "prompt_name": name,
                        "is_enabled": True,
                        "config": {"source": "langfuse", "kind": "prompt_only"},
                        "created_by": request.user,
                        "updated_by": request.user,
                    },
                )

        tools_qs = Tool.objects.filter(company=company).order_by("slug")
        data = ToolSerializer(tools_qs, many=True, context=context).data
        return Response(data, status=status.HTTP_200_OK)
