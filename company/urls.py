"""URLs for the company app's global switcher.

Mounted at ``/company/`` in ``base.urls`` BEFORE the admin include so the setter
is reachable from both the admin pages and the top-level ``/chat/`` surface.
"""

from django.urls import path

from . import views

app_name = "company"

urlpatterns = [
    path(
        "set-active-company/",
        views.set_active_company,
        name="set_active_company",
    ),
]
