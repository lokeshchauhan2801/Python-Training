"""URLs for the top-level ``/agent-builder/`` surface.

Mounted at ``/agent-builder/`` in ``base.urls`` BEFORE the admin include so the
page lives outside ``/admin/`` while still rendering inside the admin shell
(same placement as ``/chat/``).
"""

from django.urls import path

from . import views

app_name = "agents"

urlpatterns = [
    path("", views.agent_builder_page, name="agent_builder"),
]
