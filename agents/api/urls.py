"""Agents API router, mounted at ``api/v1/agents/``."""

from rest_framework.routers import SimpleRouter

from .views import (
    AgentEdgeViewSet,
    AgentGraphViewSet,
    AgentNodeViewSet,
    ToolViewSet,
)

router = SimpleRouter(trailing_slash=True)
router.register("graph", AgentGraphViewSet, basename="agent-graph")
router.register("node", AgentNodeViewSet, basename="agent-node")
router.register("edge", AgentEdgeViewSet, basename="agent-edge")
router.register("tool", ToolViewSet, basename="tool")

urlpatterns = router.urls
