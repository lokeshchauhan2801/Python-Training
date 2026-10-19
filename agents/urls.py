"""URL for the top-level ``/agent-builder/`` page (mounted in base.urls)."""

from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    path("", views.agent_builder_page, name="agent_builder"),
]
