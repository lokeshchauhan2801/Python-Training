"""The ``/agent-builder-v2/`` page: the Svelte Flow canvas inside the Unfold shell.

This is a SEPARATE module from ``views.py`` so that removing either canvas never
touches the other's view. It mirrors ``views.py::agent_builder_page`` exactly
except for the page title and the template it renders.
"""

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_sameorigin

from company.active import active_company_for_request


@login_required
@xframe_options_sameorigin
def agent_builder_v2_page(request):
    """GET /agent-builder-v2/ — the Svelte Flow canvas inside the Unfold shell.

    The active company (same session key the header switcher and ``/chat/`` use)
    is resolved here and handed to the iframe canvas via its ``src``
    querystring, so the canvas can scope its ``api/v1/agents/`` reads and stamp
    the right ``company`` on create. Without it a multi-company user hits a 400
    ("ambiguous; specify a company") because the API cannot guess which company
    a new graph belongs to.
    """
    active_company = active_company_for_request(request)
    context = {
        **admin.site.each_context(request),
        "title": "LangGraph (Svelte)",
        "active_company_id": str(active_company.id) if active_company else "",
        "active_company_name": active_company.name if active_company else "",
    }
    return render(request, "agents/agent_builder_v2.html", context)
