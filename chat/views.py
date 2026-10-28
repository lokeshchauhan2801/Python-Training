import json
import logging

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from basics.llm import (
    LLMBackendError,
    LLMClient,
    LLMConfigurationError,
    resolve_provider_model,
)
from documents.models import Document

from .scope import active_or_none, allowed_companies, scoped_sessions
from .services import ChatProviderError, ChatRetrievalError, ChatService

logger = logging.getLogger(__name__)

_ULID_LEN = 26


def _chat_service():
    """Build the chat service (lazy, no key at construct). Tests patch this."""
    return ChatService()


def _orchestrator_for(company, llm_client=None):

    def _rag_service():

        if llm_client is not None:
            return ChatService(llm_client=llm_client)
        return _chat_service()

    try:
        from agents.models import AgentGraph, AgentNode

        graph = (
            AgentGraph.objects.filter(company=company, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if graph is None:
            return _rag_service()
        has_start = AgentNode.objects.filter(
            graph=graph, kind=AgentNode.KIND_START
        ).exists()
        has_step = AgentNode.objects.filter(
            graph=graph, kind=AgentNode.KIND_STEP
        ).exists()
        if not (has_start and has_step):
            return _rag_service()

        from chat.services.langgraph_service import LangGraphChatService
        from basics.langfuse import get_langfuse_client

        return LangGraphChatService(
            chat_service=_rag_service(),
            langfuse=get_langfuse_client(),
        )
    except Exception:
        logger.debug("agents graph lookup failed; using ChatService", exc_info=True)
        return _rag_service()


def _valid_ulid(value):
    return isinstance(value, str) and len(value) == _ULID_LEN


def _resolve_session(request, session_id):
    """Resolve a session within the request's scope or raise 404.

    The ``session_id`` argument is the session's primary key, which is the
    ULID ``session_id`` field (the single session identifier).
    """
    if not _valid_ulid(session_id):
        raise Http404("Invalid session id.")
    return get_object_or_404(scoped_sessions(request), pk=session_id)


def _turn_payload(turn):
    """Serialize a Conversation turn into a ``{user, assistant}`` message pair.

    Chat history lives in two tables now: a single ``Conversation`` turn holds
    both the user's question and the assistant's response. The frontend still
    renders per-message bubbles (keyed on ``role``), so each turn is expanded
    into two message-shaped dicts preserving the old wire shape. Attachments
    render on the user bubble; citations/chart/metadata on the assistant bubble.
    The synthetic ``<ulid>:u``/``<ulid>:a`` ids are display-only (the client
    never posts a message id back).
    """
    attachments = [
        {"id": str(doc.id), "title": doc.title}
        for doc in turn.documents.all()
    ]
    created_at = turn.created_at.isoformat()
    user = {
        "id": f"{turn.id}:u",
        "role": "user",
        "content": turn.question,
        "citations": [],
        "chart_data": None,
        "metadata": {},
        "model_name": "user",
        "attachments": attachments,
        "created_at": created_at,
    }
    assistant = {
        "id": f"{turn.id}:a",
        "role": "assistant",
        "content": turn.response,
        "citations": turn.context.get("citations", []),
        "chart_data": turn.context.get("chart"),
        "metadata": turn.metadata or {},
        "model_name": turn.model_name,
        "attachments": [],
        "created_at": created_at,
    }
    return {"user": user, "assistant": assistant}


def _turns_to_messages(turns):
    """Flatten ``Conversation`` turns into a per-message list for the client.

    Each turn yields a user dict then an assistant dict (skipping the assistant
    dict when the turn has no response, which only happens for an unanswered
    turn). Order follows ``created_at``.
    """
    messages = []
    for turn in turns:
        pair = _turn_payload(turn)
        messages.append(pair["user"])
        if turn.response:
            messages.append(pair["assistant"])
    return messages


_PROVIDER_DISPLAY_NAMES = {"openai": "OpenAI", "deepseek": "DeepSeek"}


def _provider_display_name(provider):
    return _PROVIDER_DISPLAY_NAMES.get(provider, provider.capitalize())


@login_required
@require_http_methods(["GET"])
def models_json(request):
    from django.conf import settings

    provider = getattr(settings, "LLM_PROVIDER", "openai")
    registry = getattr(settings, "LLM_PROVIDERS", {}) or {}

    if not registry:

        fallback_model = getattr(settings, "LLM_MODEL", "gpt-4o-mini")
        return JsonResponse(
            {
                "provider": provider,
                "default": fallback_model,
                "default_provider": provider,
                "models": [
                    {
                        "id": fallback_model,
                        "label": "%s (%s)"
                        % (fallback_model, _provider_display_name(provider)),
                        "provider": provider,
                    }
                ],
            }
        )


    active_cfg = registry.get(provider) or {}
    display = _provider_display_name(provider)
    models = [
        {
            "id": model_id,
            "label": "%s (%s)" % (model_id, display),
            "provider": provider,
        }
        for model_id in active_cfg.get("models") or []
    ]

    default_model = (
        active_cfg.get("chat_model")
        or (active_cfg.get("models") or [None])[0]
        or getattr(settings, "LLM_MODEL", "gpt-4o-mini")
    )

    return JsonResponse(
        {
            "provider": provider,
            "default": default_model,
            "default_provider": provider,
            "models": models,
        }
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
    session = _resolve_session(request, session_id)


    active = active_or_none(request)
    in_active_company = active is not None and session.company_id == active.id
    preselect = str(session.session_id) if in_active_company else ""

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
            "id": str(session.session_id),
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
    messages = _turns_to_messages(session.turns.order_by("created_at"))
    return JsonResponse(
        {
            "id": str(session.session_id),
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
            "id": str(session.session_id),
            "title": session.title,
            "company_id": str(session.company_id),
        },
        status=201,
    )


from chat.services import kb as _kb

# ChatGPT-style phrasing when a file is attached with no message: the assistant
# asks what to do instead of auto-summarising (Option A). Kept short; the user
# can then say "summarise", "add to knowledge base", ask a question, etc.
_EMPTY_ATTACHMENT_REPLY = (
    "I see you've attached {names}. What would you like me to do with it? "
    "I can summarise it, pull out the key points, answer questions about it, "
    "create a chart from its data, or add it to the company knowledge base so "
    "it's searchable later — just tell me."
)


def _is_add_to_kb_request(text):
    """True when the message asks to add the attached file to the knowledge base."""
    return _kb.is_add_to_kb_request(text)


def _is_status_request(text):
    """True when the message asks about document processing status/progress."""
    return _kb.is_status_request(text)


def _attachment_titles(session, attachment_ids):
    """Human-readable names for the turn's attachments (for the ask-reply)."""
    docs = Document.objects.filter(pk__in=attachment_ids, company=session.company)
    titles = [d.title for d in docs]
    if not titles:
        return "a file"
    if len(titles) == 1:
        return f"“{titles[0]}”"
    return ", ".join(f"“{t}”" for t in titles[:-1]) + f" and “{titles[-1]}”"


def _turn_response(request, session, turn):
    """Serialize a stored turn as the ``{user, assistant, session}`` ``send`` shape."""
    session.refresh_from_db(fields=["title"])
    pair = _turn_payload(turn)
    return JsonResponse(
        {
            "user": pair["user"],
            "assistant": pair["assistant"],
            "session": {"id": str(session.session_id), "title": session.title},
        },
        status=201,
    )


def _reply_empty_attachment(request, session, attachment_ids):
    """Option A: store the attachment turn and ask what to do (no LLM call)."""
    service = _chat_service()
    # Empty user question — the attachment chip renders from the turn's
    # documents, matching the ChatGPT look (file card, no fabricated text).
    content = _EMPTY_ATTACHMENT_REPLY.format(
        names=_attachment_titles(session, attachment_ids)
    )
    turn = service.create_turn(
        session,
        user=session.user,
        question="",
        response=content,
        model_name="assistant",
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn)


def _add_attachments_to_knowledge_base(request, session, question, attachment_ids):
    """Promote chat attachments to library Documents and dispatch via Temporal.

    Delegates to ``chat.services.kb.add_session_attachments_to_kb`` (dedup +
    Temporal dispatch; the CHAT path always uses Temporal). Persists the user
    message and a direct assistant confirmation (no LLM call) that names the
    Temporal workflow id, and returns the pair in the shape ``send`` returns.
    When a dispatch fails (transient), the assistant message carries
    ``metadata.retry_document_ids`` so the chat can render a Retry button.
    """
    service = _chat_service()

    result = _kb.add_session_attachments_to_kb(
        conversation=session,
        company=session.company,
        attachment_ids=attachment_ids,
        actor=request.user,
    )

    turn = service.create_turn(
        session,
        user=session.user,
        question=question,
        response=result["content"],
        model_name="knowledge_base",
        metadata=_kb_reply_metadata(result),
        attachment_ids=attachment_ids,
        actor=request.user,
    )

    return _turn_response(request, session, turn)


def _kb_reply_metadata(result):
    """Map a kb.py dispatch result to the assistant message metadata keys.

    One place that maps ``result`` -> ``{retry_document_ids, temporal_links,
    live_status_document_ids}`` so both ``_add_attachments_to_knowledge_base``
    and the pending-selection seam set identical metadata.
    """
    extra_meta = {}
    if result.get("failed_ids"):
        extra_meta["retry_document_ids"] = result["failed_ids"]
    # Quick links to the Temporal workflow(s) for the dispatched file(s).
    temporal_links = [
        {"title": q["title"], "url": q["temporal_url"], "workflow_id": q["workflow_id"]}
        for q in result.get("queued", [])
        if q.get("temporal_url")
    ]
    if temporal_links:
        extra_meta["temporal_links"] = temporal_links
    # Live-track the freshly queued documents right in this reply.
    queued_ids = [q["id"] for q in result.get("queued", [])]
    if queued_ids:
        extra_meta["live_status_document_ids"] = queued_ids
    return extra_meta


def _answer_pending_kb_selection(request, session, question, attachment_ids, result):
    """Persist the user answer + deterministic assistant reply for a pending KB
    selection (dispatch/cancel/re-ask), mirroring the add-to-KB reply shape."""
    service = _chat_service()
    # A re-ask or a cancel dispatched nothing, so there is no metadata to set.
    turn = service.create_turn(
        session,
        user=session.user,
        question=question,
        response=result["content"],
        model_name="knowledge_base",
        metadata=_kb_reply_metadata(result),
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn)


def _answer_document_status(request, session, question, attachment_ids):
    """Report document processing status directly (no LLM call).

    The assistant message carries ``metadata.live_status_document_ids`` so the
    chat UI can render a live tracker that polls the status endpoint until the
    referenced documents reach a terminal state.
    """
    service = _chat_service()
    content = _kb.document_status_report(
        company=session.company, message_text=question
    )
    track_ids = _kb.status_document_ids(
        company=session.company, message_text=question
    )
    metadata = {"live_status_document_ids": track_ids} if track_ids else {}
    turn = service.create_turn(
        session,
        user=session.user,
        question=question,
        response=content,
        model_name="assistant",
        metadata=metadata,
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn)


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

    model_name = (request.POST.get("model_name") or "").strip() or None
    provider = (request.POST.get("provider") or "").strip() or None
    attachment_ids = request.POST.getlist("attachment_ids")

    # A turn needs EITHER text OR at least one attachment. Sending a media file
    # with no message is allowed; only a turn with neither is rejected.
    if not question and not attachment_ids:
        return JsonResponse(
            {"error": "Type a message or attach a file."}, status=400
        )
    # File attached with NO message (ChatGPT/Claude behaviour, Option A): don't
    # fabricate a prompt or auto-summarise — store the attachment turn and ask
    # the user what they want to do with it.
    if not question and attachment_ids:
        return _reply_empty_attachment(request, session, attachment_ids)

    # Stateful KB-selection follow-up: if we previously asked "which file(s)?"
    # and saved pending state, interpret THIS message as the selection answer
    # BEFORE the add-to-KB / router path, so "all" / "second one" / a filename
    # resolve the pending choice instead of re-triggering a brand-new add. Runs
    # for BOTH orchestration paths because send() is shared. Deterministic (no
    # LLM) — the view seam only uses the deterministic parser in kb.py.
    pending = _kb.handle_pending_selection(
        conversation=session,
        company=session.company,
        message_text=question,
        actor=request.user,
    )
    if pending is not None:
        return _answer_pending_kb_selection(
            request, session, question, attachment_ids, pending
        )

    # Intent net (works for BOTH the LangGraph router path and the plain
    # ChatService path, i.e. companies with no agent graph):
    #   * "add/process this file to the knowledge base" -> Temporal processing.
    #   * "what's processing / status of <id>"          -> status report.
    # Honour an add-to-KB request even when no file is re-attached on this turn:
    # the file is commonly uploaded on an earlier empty-text turn, so defer to
    # the session fallback in ``kb.add_session_attachments_to_kb`` to resolve it.
    if _is_add_to_kb_request(question):
        return _add_attachments_to_knowledge_base(
            request, session, question, attachment_ids
        )
    if _is_status_request(question):
        return _answer_document_status(request, session, question, attachment_ids)

    resolved_provider, resolved_model = resolve_provider_model(model_name, provider)


    selected = bool(model_name or provider)
    llm_client = (
        LLMClient(provider=resolved_provider, model=resolved_model)
        if selected
        else None
    )


    chat_user = session.user

    try:
        turn = _orchestrator_for(
            session.company, llm_client=llm_client
        ).process_message(
            user=chat_user,
            message_text=question,
            conversation=session,
            company=session.company,
            attachment_ids=attachment_ids or None,


            model_name=resolved_model if selected else model_name,
        )
    except LLMConfigurationError:
        return JsonResponse({"error": "LLM not configured"}, status=503)
    except (LLMBackendError, ChatProviderError):
        return JsonResponse({"error": "LLM request failed"}, status=502)
    except ChatRetrievalError:
        return JsonResponse({"error": "Document retrieval failed"}, status=502)
    except ValueError:


        return JsonResponse({"error": "This chat is not available."}, status=400)

    session.refresh_from_db(fields=["title"])

    pair = _turn_payload(turn)
    return JsonResponse(
        {
            "user": pair["user"],
            "assistant": pair["assistant"],
            "session": {"id": str(session.session_id), "title": session.title},
        },
        status=201,
    )


@login_required
@require_http_methods(["POST"])
def delete_session(request, session_id):
    """Delete one session (and its turns) within the request's scope.

    PERMANENT by default: the session and its turns are removed with a real SQL
    DELETE (``hard_delete``). Soft delete (``is_deleted=True``, kept for audit)
    happens ONLY when the client explicitly opts in by sending ``soft=1`` (POST
    body or querystring). Scoped like every other chat endpoint (out-of-scope
    => 404).
    """
    session = _resolve_session(request, session_id)

    soft = (request.POST.get("soft") or request.GET.get("soft") or "").strip() in (
        "1",
        "true",
        "yes",
        "on",
    )

    if soft:
        session.turns.all().delete(actor=request.user)
        session.delete(actor=request.user)
    else:
        # Permanent removal. Hard-delete the session's turns first, then the
        # session row itself. Use all_objects so already soft-deleted rows are
        # purged too.
        from .models import Conversation

        Conversation.all_objects.filter(session=session).hard_delete()
        session.hard_delete()

    return JsonResponse(
        {"id": str(session.session_id), "deleted": True, "soft": soft}
    )


@login_required
@require_http_methods(["POST"])
def reprocess_document(request, session_id):
    """Retry Temporal processing for a failed chat knowledge-base document.

    Backs the chat "Retry" button. ``document_id`` is read from the POST body
    (or JSON) and scoped to the session's company, so a user can never retry
    another company's document. Returns the new workflow id on success.
    """
    session = _resolve_session(request, session_id)

    document_id = request.POST.get("document_id")
    if document_id is None and request.body:
        try:
            document_id = (json.loads(request.body) or {}).get("document_id")
        except (ValueError, TypeError):
            document_id = None
    document_id = (document_id or "").strip()
    if not document_id:
        return JsonResponse({"error": "A document_id is required."}, status=400)

    result = _kb.retry_document_processing(
        document_id=document_id, company=session.company, actor=request.user
    )
    return JsonResponse(
        {
            "ok": result["ok"],
            "message": result["content"],
            "workflow_id": result.get("workflow_id"),
            "document_id": document_id,
        },
        status=200 if result["ok"] else 502,
    )


@login_required
@require_http_methods(["GET"])
def document_status_json(request, session_id):
    """Live status rows for the chat tracker (polled by a status reply).

    Query params:
      * ``ids`` — comma-separated document ids to report (what a reply tracks).
    Falls back to the company's in-flight documents when no ids are given.
    Scoped to the session's company. Returns ``{documents: [...], all_done}``.
    """
    session = _resolve_session(request, session_id)

    ids_param = (request.GET.get("ids") or "").strip()
    document_ids = [i for i in ids_param.split(",") if i] if ids_param else None

    rows = _kb.document_status_rows(
        company=session.company,
        document_ids=document_ids,
        message_text=None if document_ids else "",
    )
    all_done = all(row["terminal"] for row in rows) if rows else True
    return JsonResponse({"documents": rows, "all_done": all_done})


@login_required
@require_http_methods(["POST"])
def attach(request, session_id):
    """Store an uploaded file as a chat Document (not chunked) on the session.

    The document is stamped with the signed-in user and the session's company
    (the invariant chat attachments require), so one user can never attach
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
