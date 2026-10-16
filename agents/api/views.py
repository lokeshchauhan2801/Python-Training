"""Views for the agents API.

Four owner/company-scoped viewsets (``ScopedModelViewSet``) back the canvas:
``AgentGraphViewSet``, ``AgentNodeViewSet``, ``AgentEdgeViewSet``, and
``ToolViewSet``. Scoping means out-of-scope ids resolve to **404** (superuser
sees all; everyone else sees rows whose ``company.owner`` is the request user),
exactly like ``documents``/``chat``.

``AgentGraphViewSet`` adds two extra actions:

* ``GET /graph/{id}/load/`` — the aggregated canvas payload
  ``{graph, nodes, edges, tools}`` in one response.
* ``POST /graph/{id}/activate/`` — set this graph active. Inside
  ``transaction.atomic()`` the company's currently-active graph is cleared
  FIRST, then the target is set active, so the partial unique constraint
  (``agents_one_active_graph_per_company``) is never transiently violated.
"""

from django.db import transaction
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
