"""Agents API router, mounted at ``api/v1/agents/``."""

from rest_framework.routers import SimpleRouter

from .views import AgentViewSet, StepViewSet, ToolViewSet

router = SimpleRouter(trailing_slash=True)
router.register("tool", ToolViewSet, basename="tool")
router.register("agent", AgentViewSet, basename="agent")
router.register("step", StepViewSet, basename="step")

urlpatterns = router.urls
