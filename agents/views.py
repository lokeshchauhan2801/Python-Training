"""Top-level ``/agent-builder/`` surface.

A single Django view rendered inside the Unfold admin shell (it pulls
``admin.site.each_context`` so the LEFT sidebar + branding match the admin,
exactly like ``chat.views.chat_home``). The content area is one same-origin
``<iframe>`` hosting the React Flow canvas (built/vendored under
``agents/static/agents/canvas/``). The canvas talks to the DRF
``api/v1/agents/`` endpoints same-origin with the session cookie + the CSRF
token the page exposes to the iframe via its ``src`` querystring.

Security: ``login_required`` gates the page; ``xframe_options_sameorigin``
stamps ``X-Frame-Options: SAMEORIGIN`` on the response so the Unfold shell may
frame the same-origin canvas (the project-wide default is SAMEORIGIN too, but
the decorator makes the intent explicit on this view).
"""

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_sameorigin


@login_required
@xframe_options_sameorigin
def agent_builder_page(request):
    """GET /agent-builder/ — the React Flow canvas inside the Unfold shell."""
    context = {
        **admin.site.each_context(request),
        "title": "LangGraph",
    }
    return render(request, "agents/agent_builder.html", context)
