"""URL for the top-level ``/agent-builder-v2/`` page (mounted in base.urls).

A SEPARATE urlconf from ``urls.py`` so the Svelte canvas can be removed without
touching the React canvas's routing.
"""

from django.urls import path

from . import views_v2

app_name = "agents_v2"

urlpatterns = [
    path("", views_v2.agent_builder_v2_page, name="agent_builder_v2"),
]
