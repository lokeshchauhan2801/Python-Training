"""Root URL configuration for the agentic-flow project.

The admin (django-unfold) is the primary UI; a thin DRF API lives under
``api/v1/<app>/`` with each app owning its own ``api/urls.py``.

``temporal/`` is the staff-only proxy to the internal Temporal Web UI.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from infrastructure.temporal.ui_proxy import temporal_ui_proxy

urlpatterns = [


    path("chat/", include("chat.urls")),


    path("agent-builder/", include("agents.urls")),


    path("company/", include("company.urls")),
    path("admin/", admin.site.urls),
    re_path(r"^temporal/(?P<path>.*)$", temporal_ui_proxy, name="temporal_ui"),
    path("api/v1/auth/", include("auth.api.urls")),
    path("api/v1/company/", include("company.api.urls")),
    path("api/v1/chat/", include("chat.api.urls")),
    path("api/v1/scrapper/", include("scrapper.api.urls")),
    path("api/v1/documents/", include("documents.api.urls")),
    path("api/v1/agents/", include("agents.api.urls")),
]
