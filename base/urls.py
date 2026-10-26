"""Root URL configuration for the agentic-flow project.

The admin (django-unfold) is the primary UI; a thin DRF API lives under
``api/v1/<app>/`` with each app owning its own ``api/urls.py``.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [


    path("chat/", include("chat.urls")),


    path("agent-builder/", include("agents.urls")),


    path("company/", include("company.urls")),
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("auth.api.urls")),
    path("api/v1/company/", include("company.api.urls")),
    path("api/v1/chat/", include("chat.api.urls")),
    path("api/v1/scrapper/", include("scrapper.api.urls")),
    path("api/v1/documents/", include("documents.api.urls")),
    path("api/v1/agents/", include("agents.api.urls")),
]
