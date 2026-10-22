"""Top-level ``/chat/`` surface (option B).

Plain Django views rendered inside the Unfold admin shell (they pull
``admin.site.each_context`` so the LEFT sidebar + branding match the admin). The
page itself is wired entirely with AJAX — no full-page navigation — and all
server-side scope is re-derived here (never trusted from the client) via the
shared helpers in ``chat.scope``.

JSON endpoints:
  * ``companies_json`` / ``sessions_json`` / ``messages_json`` — read models.
  * ``new_session`` — AJAX create, always JSON 201, NEVER a redirect (BUG 1).
  * ``send`` — runs ``ChatService.process_message`` and returns the persisted
    user + assistant ``Message`` pair; the first send retitles the session from
    the message (BUG 2, done in the service).
  * ``attach`` — stores an uploaded file as a ``source=chat`` Document (not
    chunked) linked to the session.

Security: ``login_required`` on every view; out-of-scope company/session ids
resolve to 404 via the scoped querysets; POSTs are CSRF-protected (no
``csrf_exempt``). The client renders server data with
``textContent``/``createElement``/``appendChild`` only.
"""

import json
import logging

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from basics.llm import LLMBackendError, LLMConfigurationError
from documents.models import Document

from .scope import active_or_none, allowed_companies, scoped_sessions
from .services import ChatProviderError, ChatRetrievalError, ChatService

logger = logging.getLogger(__name__)

_ULID_LEN = 26


def _chat_service():
    """Build the chat service (lazy, no key at construct). Tests patch this."""
    return ChatService()


def _orchestrator_for(company):
    """Return the service a NEW message runs through.

    ``LangGraphChatService`` when ``company`` has an active, runnable graph
    (an active ``AgentGraph`` with a START node and at least one STEP node);
    otherwise the existing ``ChatService`` (``_chat_service()``), so chat is
    unchanged where no graph is configured.

    Import-safety: ``agents`` is imported LAZILY here so ``chat`` has no
    import-time dependency on ``agents`` (and none on ``langgraph``). ANY
    import/lookup error degrades silently to the RAG path — a missing or
    unrunnable graph is never an error, just a fallback.
    """
    try:
        from agents.models import AgentGraph, AgentNode

        graph = (
            AgentGraph.objects.filter(company=company, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if graph is None:
            return _chat_service()
        has_start = AgentNode.objects.filter(
            graph=graph, kind=AgentNode.KIND_START
        ).exists()
        has_step = AgentNode.objects.filter(
            graph=graph, kind=AgentNode.KIND_STEP
        ).exists()
        if not (has_start and has_step):
            return _chat_service()

        from chat.services.langgraph_service import LangGraphChatService
        from basics.langfuse import get_langfuse_client

        return LangGraphChatService(
            chat_service=ChatService(),
            langfuse=get_langfuse_client(),
        )
    except Exception:
        logger.debug("agents graph lookup failed; using ChatService", exc_info=True)
        return _chat_service()


def _valid_ulid(value):
    return isinstance(value, str) and len(value) == _ULID_LEN


def _resolve_session(request, session_id):
    """Resolve a session within the request's scope or raise 404."""
    if not _valid_ulid(session_id):
        raise Http404("Invalid session id.")
    return get_object_or_404(scoped_sessions(request), pk=session_id)


def _message_payload(message):
    """Serialize a Message for the client (ordered, display-ready)."""
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "citations": message.citations or [],
        "chart_data": message.chart_data,
        "metadata": message.metadata or {},
        "model_name": message.model_name,
        "created_at": message.created_at.isoformat(),
    }


@login_required
@require_http_methods(["GET"])
def models_json(request):
    """Return the ACTIVE LLM provider and the models the dropdown should offer.

    Auto-detects the provider from settings.LLM_PROVIDER and reads that
    provider's model list from settings.LLM_PROVIDERS. The chat composer
    populates its model <select> from this (no hardcoded model list), so
    switching LLM_PROVIDER (openai <-> deepseek) changes the offered models
    automatically. Degrades to the generic LLM_MODEL when no registry entry.
    """
    from django.conf import settings

    provider = getattr(settings, "LLM_PROVIDER", "openai")
    registry = getattr(settings, "LLM_PROVIDERS", {}) or {}
    cfg = registry.get(provider) or {}
    models = cfg.get("models") or [getattr(settings, "LLM_MODEL", "gpt-4o-mini")]
    default_model = cfg.get("chat_model") or models[0]
    return JsonResponse(
        {"provider": provider, "models": models, "default": default_model}
    )


@login_required
def chat_home(request):
    """GET /chat/ — the three-area chat page with no session preselected."""
    context = {
        **admin.site.each_context(request),
        "title": "New Chat",
        "preselect_session_id": "",
    }
    return render(request, "chat/chat_page.html", context)


@login_required
def chat_session(request, session_id):
    """GET /chat/<ulid>/ — same page, auto-opening the given session.

    The session is auto-opened ONLY when it belongs to the request's ACTIVE
    company (the global header switcher). Owning the session is not enough: a
    user owns sessions across every company they own, so after switching the
    active company the URL may still carry a session from the PREVIOUS company.
    Auto-opening it then would show the same conversation under a different
    company. When the session is out of the active company we fall through to a
    blank page (like ``chat_home``) so the panel resets to a New Chat and the
    left list reflects the newly active company.
    """
    if not _valid_ulid(session_id):
        raise Http404("Invalid session id.")

    session = get_object_or_404(scoped_sessions(request), pk=session_id)


    active = active_or_none(request)
    in_active_company = active is not None and session.company_id == active.id
    preselect = str(session.id) if in_active_company else ""

    context = {
        **admin.site.each_context(request),
        "title": "New Chat",
        "preselect_session_id": preselect,
    }
    return render(request, "chat/chat_page.html", context)


@login_required
@require_http_methods(["GET"])
def companies_json(request):
    companies = [
        {"id": str(company.id), "name": company.name}
        for company in allowed_companies(request).order_by("name")
    ]
    return JsonResponse({"companies": companies})


@login_required
@require_http_methods(["GET"])
def sessions_json(request):
    sessions = scoped_sessions(request)

    company_id = request.GET.get("company")
    if company_id:

        if not allowed_companies(request).filter(pk=company_id).exists():
            return JsonResponse({"sessions": []})
        sessions = sessions.filter(company_id=company_id)

    payload = [
        {
            "id": str(session.id),
            "session_id": session.session_id,
            "title": session.title,
            "company_id": str(session.company_id),
            "created_at": session.created_at.isoformat(),
        }
        for session in sessions
    ]
    return JsonResponse({"sessions": payload})


@login_required
@require_http_methods(["GET"])
def messages_json(request, session_id):
    session = _resolve_session(request, session_id)
    messages = [
        _message_payload(message)
        for message in session.messages.order_by("created_at")
    ]
    return JsonResponse(
        {
            "id": str(session.id),
            "session_id": session.session_id,
            "title": session.title,
            "company_id": str(session.company_id),
            "messages": messages,
        }
    )


@login_required
@require_http_methods(["POST"])
def new_session(request):
    """Create one session and return it as JSON 201 (NEVER a redirect).

    The session keeps the model default title ('New session') so the first
    ``send`` retitles it from the message (BUG 2). The client appends the
    returned session in-place and pushes /chat/<id>/ — it never navigates the
    browser to this JSON endpoint (BUG 1).
    """
    company_id = request.POST.get("company")
    allowed = allowed_companies(request)
    if not allowed.exists():
        return JsonResponse({"error": "No company available."}, status=400)

    if company_id:

        company = get_object_or_404(allowed, pk=company_id)
    else:


        company = active_or_none(request) or allowed.order_by("name").first()

    from .models import ConversationSession

    session = ConversationSession(company=company, user=request.user)
    session.save(actor=request.user)

    return JsonResponse(
        {
            "id": str(session.id),
            "session_id": session.session_id,
            "title": session.title,
            "company_id": str(session.company_id),
        },
        status=201,
    )


@login_required
@require_http_methods(["POST"])
def send(request, session_id):
    """Run the RAG pipeline for one message; return the user+assistant pair."""
    session = _resolve_session(request, session_id)

    question = request.POST.get("question")
    if question is None and request.body:
        try:
            question = (json.loads(request.body) or {}).get("question")
        except (ValueError, TypeError):
            question = None
    question = (question or "").strip()
    if not question:
        return JsonResponse({"error": "Question is required."}, status=400)

    model_name = (request.POST.get("model_name") or "").strip() or None
    attachment_ids = request.POST.getlist("attachment_ids")


    chat_user = session.user

    try:
        assistant_message = _orchestrator_for(session.company).process_message(
            user=chat_user,
            message_text=question,
            conversation=session,
            company=session.company,
            attachment_ids=attachment_ids or None,
            model_name=model_name,
        )
    except LLMConfigurationError:
        return JsonResponse({"error": "LLM not configured"}, status=503)
    except (LLMBackendError, ChatProviderError):
        return JsonResponse({"error": "LLM request failed"}, status=502)
    except ChatRetrievalError:
        return JsonResponse({"error": "Document retrieval failed"}, status=502)
    except ValueError:


        return JsonResponse({"error": "This chat is not available."}, status=400)

    user_message = (
        session.messages.filter(role="user").order_by("-created_at").first()
    )


    session.refresh_from_db(fields=["title"])

    return JsonResponse(
        {
            "user": _message_payload(user_message) if user_message else None,
            "assistant": _message_payload(assistant_message),
            "session": {"id": str(session.id), "title": session.title},
        },
        status=201,
    )


@login_required
@require_http_methods(["POST"])
def delete_session(request, session_id):
    """Soft-delete one session (and its messages) within the request's scope.

    Scoped like every other chat endpoint (out-of-scope => 404). Uses the
    SoftDeleteModel API (``is_deleted=True`` via ``delete``), never a hard
    DELETE, and soft-deletes the session's messages for consistency.
    """
    session = _resolve_session(request, session_id)


    session.messages.all().delete(actor=request.user)
    session.delete(actor=request.user)

    return JsonResponse({"id": str(session.id), "deleted": True})


@login_required
@require_http_methods(["POST"])
def attach(request, session_id):
    """Store an uploaded file as a chat Document (not chunked) on the session.

    The document is stamped with the signed-in user and the session's company
    (the invariant Message attachments require), so one user can never attach
    into another company's session.
    """
    session = _resolve_session(request, session_id)

    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "A file is required."}, status=400)

    file_type = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "").lower()

    document = Document(
        company=session.company,
        uploaded_by=request.user,
        session=session,
        source=Document.SOURCE_CHAT,
        status=Document.STATUS_READY,
        title=upload.name,
        file=upload,
        file_type=file_type or "txt",
        content_type=getattr(upload, "content_type", "") or "",
        file_size=getattr(upload, "size", 0) or 0,
    )
    document.save(actor=request.user)

    return JsonResponse(
        {
            "id": str(document.id),
            "title": document.title,
            "file_type": document.file_type,
        },
        status=201,
    )
