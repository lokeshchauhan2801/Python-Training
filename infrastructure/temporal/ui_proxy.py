"""Reverse proxy for the Temporal Web UI at /temporal/ — active staff only.

Temporal UI has no auth of its own and no host port; the only host entry is
the nginx gate on 127.0.0.1:8080 (TEMPORAL_UI_URL), which returns 403 unless
the request carries TEMPORAL_UI_PROXY_SECRET. This view is the only caller
that adds it, and only after the login + staff check, so it is the only way
in. The UI runs with TEMPORAL_UI_PUBLIC_PATH=/temporal, so the path is
forwarded unchanged. Django's own session/CSRF cookies, any Authorization
header and any browser-sent gate header are stripped before forwarding; every
Set-Cookie from Temporal UI is passed back. csrf_exempt applies to this view
only: Temporal UI enforces its own CSRF check on the forwarded requests.
Full flow: TEMPORAL_FLOW.md.
"""

import logging
from http.cookies import SimpleCookie

import requests
from requests.adapters import HTTPAdapter
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, StreamingHttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
# requests decodes gzip itself (iter_content), so encoding headers are dropped
# both ways; Cookie is rebuilt without Django's cookies.
SKIP_REQUEST_HEADERS = HOP_BY_HOP | {
    "host",
    "content-length",
    "accept-encoding",
    "content-encoding",
    "cookie",
    "authorization",
    "x-temporal-ui-gate",
}
SKIP_RESPONSE_HEADERS = HOP_BY_HOP | {"content-length", "content-encoding", "set-cookie"}

# Temporal UI serves its JS/CSS from _app/immutable/ under content-hashed names
# but sends no cache headers, so the browser re-downloaded ~150 files through
# this proxy on every page load (6-10 s). Those files never change, so the
# browser may keep them; "private" because they sit behind the staff login.
IMMUTABLE_PREFIX = "_app/immutable/"
IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"

# One pooled session: keep-alive connections to the gate instead of a new TCP
# connection per request.
_upstream = requests.Session()
_upstream.mount(
    "http://", HTTPAdapter(pool_connections=4, pool_maxsize=32)
)


def _django_cookie_names():
    return {settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME, "messages"}


def _upstream_cookie_header(request):
    blocked = _django_cookie_names()
    kept = [
        part.strip()
        for part in request.META.get("HTTP_COOKIE", "").split(";")
        if part.strip() and part.split("=", 1)[0].strip() not in blocked
    ]
    return "; ".join(kept)


def _stream(upstream):
    try:
        yield from upstream.iter_content(chunk_size=8192)
    finally:
        upstream.close()


@csrf_exempt
def temporal_ui_proxy(request, path=""):
    user = request.user
    if not user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("admin:login"))
    if not (user.is_active and user.is_staff):
        raise PermissionDenied

    base = settings.TEMPORAL_UI_URL
    url = f"{base}{settings.TEMPORAL_UI_PUBLIC_PATH}/{path}"
    if request.META.get("QUERY_STRING"):
        url = f"{url}?{request.META['QUERY_STRING']}"

    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in SKIP_REQUEST_HEADERS
    }
    cookie = _upstream_cookie_header(request)
    if cookie:
        headers["Cookie"] = cookie
    # The gate in front of Temporal UI refuses requests without this secret.
    headers["X-Temporal-UI-Gate"] = settings.TEMPORAL_UI_PROXY_SECRET

    try:
        upstream = _upstream.request(
            request.method,
            url,
            headers=headers,
            data=request.body or None,
            stream=True,
            allow_redirects=False,
            timeout=(
                settings.TEMPORAL_UI_PROXY_CONNECT_TIMEOUT_SECONDS,
                settings.TEMPORAL_UI_PROXY_TIMEOUT_SECONDS,
            ),
        )
    except requests.RequestException as exc:
        logger.warning("Temporal UI unreachable at %s: %s", base, exc.__class__.__name__)
        return HttpResponse(
            "Temporal UI is unavailable right now. Django itself is fine; try again shortly.",
            status=502,
            content_type="text/plain",
        )

    response = StreamingHttpResponse(_stream(upstream), status=upstream.status_code)
    for name, value in upstream.headers.items():
        if name.lower() in SKIP_RESPONSE_HEADERS:
            continue
        if name.lower() == "location" and value.startswith(base):
            # Keep the browser on Django, never on the internal address.
            value = value[len(base):] or "/"
        response[name] = value
    if upstream.status_code == 200 and path.startswith(IMMUTABLE_PREFIX):
        response["Cache-Control"] = IMMUTABLE_CACHE_CONTROL

    blocked = _django_cookie_names()
    for raw in upstream.raw.headers.getlist("Set-Cookie"):
        parsed = SimpleCookie()
        parsed.load(raw)
        for name, morsel in parsed.items():
            if name not in blocked:
                response.cookies[name] = morsel
    return response
