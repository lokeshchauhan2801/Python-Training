import json
import logging
from functools import partial

from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from basics.llm import (
    LLMBackendError,
    LLMClient,
    LLMConfigurationError,
    resolve_provider_model,
)
from .scope import active_or_none, allowed_companies, scoped_sessions
from .services import ChatProviderError, ChatRetrievalError, ChatService
from .services import session_files

logger = logging.getLogger(__name__)

_ULID_LEN = 26


def _chat_service():
    """Build the chat service (lazy, no key at construct). Tests patch this."""
    return ChatService()


def _rag_service(llm_client=None):
    """The sole factory for the normal (non-LangGraph) Chat service.

    Both unselected sessions and selected-Agent read-only fallbacks construct
    their service here, so there is exactly one non-LangGraph path. It imports
    or queries no Agent model.
    """
    if llm_client is not None:
        return ChatService(llm_client=llm_client)
    return _chat_service()


def _orchestrator_for(company, selected_agent=None, llm_client=None):
    """Return the service that runs a turn.

    ``selected_agent is None`` -> Normal RAG (``_rag_service``). An explicit
    resolved Agent -> a ``LangGraphChatService`` bound to that Agent. ``send``
    has already preflighted a runnable chain, so this never re-resolves the
    selection or downgrades an explicit Agent to Normal RAG. A construction /
    import failure is logged and raised as ``ChatProviderError`` so the
    existing 502 mapping applies.
    """
    if selected_agent is None:
        return _rag_service(llm_client)

    try:
        from chat.services.langgraph_service import LangGraphChatService
        from basics.langfuse import get_langfuse_client

        return LangGraphChatService(
            chat_service=_rag_service(llm_client),
            langfuse=get_langfuse_client(),
            agent=selected_agent,
        )
    except Exception as exc:
        logger.exception("Could not construct the selected-agent runtime")
        raise ChatProviderError(
            "The selected agent runtime could not be constructed."
        ) from exc


def _resolve_selected_agent(session, *, actor=None, repair=False):
    """Resolve the session's live, same-company selected Agent.

    Returns ``(agent, selection_metadata)``:

    * a live Agent and ``None`` provenance when the stored FK resolves;
    * ``(None, None)`` when the field is NULL;
    * ``(None, provenance)`` (or ``(None, None)`` when ``repair`` is false) when
      a non-null FK is not live / not same-company. With ``repair=True`` the
      stale value is cleared (audit-stamped) and a single warning is logged with
      the session and old Agent ID but no user content.
    """
    from agents.models import Agent

    old_id = session.selected_agent_id
    if old_id is None:
        return None, None
    agent = Agent.objects.filter(pk=old_id, company=session.company).first()
    if agent is not None:
        return agent, None
    # Stale / soft-deleted / cross-company persisted FK.
    if repair:
        session.selected_agent = None
        session.save(
            actor=actor,
            update_fields=["selected_agent", "updated_at", "updated_by"],
        )
        logger.warning(
            "Cleared stale selected_agent %s on session %s", old_id, session.pk
        )
        return None, {
            "agent_selection": {
                "agent_id": str(old_id),
                "agent_slug": None,
                "outcome": "fallback",
                "intent": "NONE",
                "reason": "selected_agent_unavailable",
            }
        }
    return None, None


def _selected_agent_id(session):
    """The session's live same-company selected Agent id, or ``None`` (no repair)."""
    agent, _ = _resolve_selected_agent(session)
    return str(agent.pk) if agent is not None else None


def _require_selected_agent_capability(request, session, intent):
    """403 when the session's selected Agent cannot perform ``intent``, else None.

    Guards the panel/control endpoints that act on documents without a chat
    message. No live selection (Normal RAG) keeps the endpoint's behaviour.
    """
    from chat.services import agent_capabilities

    agent, _ = _resolve_selected_agent(session, actor=request.user, repair=True)
    if agent is None:
        return None
    decision = agent_capabilities.preflight_for_intent(agent, session, intent)
    if decision.outcome == "blocked":
        return JsonResponse({"error": decision.reply}, status=403)
    return None


_AGENT_UNAVAILABLE = "Agent not found or unavailable for this company."


class _AgentChoiceError(Exception):
    pass


def _parse_agent_choice(raw, company):
    """Validate a submitted ``agent_id`` for ``company``.

    ``None`` / an empty form value clears the selection (returns ``None``); a
    26-char ULID must name a live Agent of ``company``. Raises
    ``_AgentChoiceError`` with the client-facing message otherwise.
    """
    from agents.models import Agent

    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not _valid_ulid(raw.strip()):
        raise _AgentChoiceError("agent_id must be a ULID or null")
    agent = Agent.objects.filter(company=company, pk=raw.strip()).first()
    if agent is None:
        raise _AgentChoiceError(_AGENT_UNAVAILABLE)
    return agent


def _request_data(request):
    """The POST form, or the JSON body when the request sent JSON."""
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return request.POST


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

    Chips come from ``turn.context["attachments"]``; turns written before
    that key existed fall back to the ``documents`` M2M.
    """
    chips = (turn.context or {}).get("attachments")
    if isinstance(chips, list):
        attachments = [
            {"id": str(chip.get("id", "")), "title": str(chip.get("title", ""))}
            for chip in chips
            if isinstance(chip, dict)
        ]
    else:
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
    session_files.reconcile(session)
    messages = _turns_to_messages(session.turns.order_by("created_at"))
    return JsonResponse(
        {
            "id": str(session.session_id),
            "title": session.title,
            "company_id": str(session.company_id),
            "selected_agent_id": _selected_agent_id(session),
            "messages": messages,
        }
    )


@login_required
@require_http_methods(["GET"])
def agents_json(request):
    """The company's live Agents for the chat Agent selector."""
    from agents.models import Agent

    allowed = allowed_companies(request)
    company_id = request.GET.get("company")
    if company_id:
        company = allowed.filter(pk=company_id).first()
        if company is None:
            return JsonResponse({"error": "Company not found."}, status=404)
    else:
        active = active_or_none(request)
        company = (
            active
            if active is not None and allowed.filter(pk=active.pk).exists()
            else None
        )
        if company is None:
            return JsonResponse({"error": "Select a company first."}, status=400)

    agents = [
        {"id": str(agent.pk), "name": agent.name, "slug": agent.slug}
        for agent in Agent.objects.filter(company=company).order_by(
            "order", "created_at"
        )
    ]
    return JsonResponse({"agents": agents})


@login_required
@require_http_methods(["POST"])
def selected_agent(request, session_id):
    """Set (``agent_id``) or clear (``null`` / empty) the session's Agent."""
    session = _resolve_session(request, session_id)
    data = _request_data(request)
    if data is None or "agent_id" not in data:
        return JsonResponse({"error": "agent_id is required."}, status=400)
    raw = data.get("agent_id")
    if raw == "" and request.content_type == "application/json":
        # JSON clears with null; a blank string is a malformed id.
        return JsonResponse({"error": "agent_id must be a ULID or null"}, status=400)
    try:
        agent = _parse_agent_choice(raw, session.company)
    except _AgentChoiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    session.selected_agent = agent
    session.save(
        actor=request.user,
        update_fields=["selected_agent", "updated_at", "updated_by"],
    )
    return JsonResponse(
        {
            "id": str(session.session_id),
            "company_id": str(session.company_id),
            "selected_agent_id": str(agent.pk) if agent is not None else None,
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
        # The global switcher may name a membership-only company, which is
        # outside the chat scope; use it only when it is an allowed company.
        active = active_or_none(request)
        company = (
            active
            if active is not None and allowed.filter(pk=active.pk).exists()
            else allowed.order_by("name").first()
        )

    try:
        agent = _parse_agent_choice(request.POST.get("agent_id"), company)
    except _AgentChoiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    from .models import ConversationSession

    session = ConversationSession(
        company=company, user=request.user, selected_agent=agent
    )
    session.save(actor=request.user)

    return JsonResponse(
        {
            "id": str(session.session_id),
            "title": session.title,
            "company_id": str(session.company_id),
            "selected_agent_id": str(agent.pk) if agent is not None else None,
        },
        status=201,
    )


from chat.services import file_commands, file_matching
from chat.services import kb as _kb
from documents import controls
from documents.models import Document

# ChatGPT-style phrasing when a file is attached with no message: the assistant
# asks what to do instead of auto-summarising (Option A). Kept short; the user
# can then say "summarise", "add to knowledge base", ask a question, etc.
_EMPTY_ATTACHMENT_REPLY = (
    "I see you've attached {names}. What would you like me to do with it? "
    "I can summarise it, pull out the key points, answer questions about it, "
    "create a chart from its data, or add it to the company knowledge base so "
    "it's searchable later — just tell me."
)


def _attachment_titles(session, attachment_ids):
    """Human-readable names for the turn's attachments (for the ask-reply)."""
    titles = [
        chip["title"]
        for chip in session_files.attachment_chips(session, attachment_ids)
    ]
    if not titles:
        return "a file"
    if len(titles) == 1:
        return f"“{titles[0]}”"
    return ", ".join(f"“{t}”" for t in titles[:-1]) + f" and “{titles[-1]}”"


def _turn_response(request, session, turn, selection_consumed=False):
    """Serialize a stored turn as the ``send`` shape.

    ``{user, assistant, session, selection_consumed}``; ``selection_consumed``
    tells the page to clear the picker's checkboxes.
    """
    session.refresh_from_db(fields=["title"])
    pair = _turn_payload(turn)
    return JsonResponse(
        {
            "user": pair["user"],
            "assistant": pair["assistant"],
            "session": {
                "id": str(session.session_id),
                "title": session.title,
                "selected_agent_id": _selected_agent_id(session),
            },
            "selection_consumed": bool(selection_consumed),
        },
        status=201,
    )


def _reply_empty_attachment(request, session, attachment_ids, *, selection_metadata=None):
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
        metadata={**(selection_metadata or {})},
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn)


def _command_turn(
    request, session, question, attachment_ids, *, kind, entry_ids, consumed,
    selection_metadata=None,
):
    """Store a deterministic file-command turn (no LLM) and return it."""
    reply = file_commands.handle_file_command(
        session=session,
        kind=kind,
        message=question,
        attachment_ids=attachment_ids,
        entry_ids=entry_ids,
        actor=request.user,
    )
    turn = _chat_service().create_turn(
        session,
        user=session.user,
        question=question,
        response=reply.content,
        model_name="knowledge_base",
        # Policy provenance merged LAST so it wins over a conflicting tool key.
        metadata={**reply.metadata, **(selection_metadata or {})},
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn, selection_consumed=consumed)


def _chart_target_reply(request, session, question, attachment_ids, *, selection_metadata=None):
    """``send`` step 6: an ambiguous / not-found chart target, or None.

    The reply is deterministic (no LLM, no file panel, no state), so the next
    message routes normally (D8). A ``file`` or ``no_files`` target returns
    None and the orchestrator charts it.
    """
    target = ChatService._chart_target(session, question)
    if target is None or target.kind not in ("ambiguous", "not_found"):
        return None
    turn = _chat_service().create_turn(
        session,
        user=session.user,
        question=question,
        response=file_matching.chart_target_reply(
            target, session_files.list_files(session)
        ),
        model_name="knowledge_base",
        metadata={**(selection_metadata or {})},
        attachment_ids=attachment_ids,
        actor=request.user,
    )
    return _turn_response(request, session, turn)


def _file_command_reply(
    request, session, question, attachment_ids, selected, *, selection_metadata=None
):
    """``send`` steps 2–6 (design F.1): a file-command turn, or None.

    None means the message is not a file command for this session and goes to
    the orchestrator. Tests of the LangGraph tool path patch this wrapper.
    ``selection_metadata`` (selected-Agent fallback / stale provenance) is
    merged last onto whichever direct turn is produced.
    """
    Kind = file_commands.Kind
    cmd = file_commands.classify(question)

    # 3. A picker selection with no text, or with a process command.
    if selected and (not question or cmd.kind == Kind.PROCESS):
        if not question:
            titles = [
                f.title for f in session_files.files_by_ids(session, selected)
            ]
            question = "Process: " + ", ".join(titles)
        return _command_turn(
            request,
            session,
            question,
            attachment_ids,
            kind=Kind.PROCESS,
            entry_ids=selected,
            consumed=True,
            selection_metadata=selection_metadata,
        )

    # 4. A file attached with no message: ask what to do.
    if not question and attachment_ids:
        return _reply_empty_attachment(
            request, session, attachment_ids, selection_metadata=selection_metadata
        )

    # 5. A STOP / RESUME / STATUS / PROCESS / LIST command for this session.
    kind = file_commands.effective_kind(cmd, session)
    if kind is None:
        return _chart_target_reply(
            request, session, question, attachment_ids,
            selection_metadata=selection_metadata,
        )
    # STOP and LIST ignore the selection: in-flight rows have no checkbox (V12).
    entry_ids = selected if selected and kind in (Kind.RESUME, Kind.STATUS) else None
    return _command_turn(
        request,
        session,
        question,
        attachment_ids,
        kind=kind,
        entry_ids=entry_ids,
        consumed=bool(entry_ids),
        selection_metadata=selection_metadata,
    )


def _pipeline_direct_reply(
    request, session, question, attachment_ids, selected, *, agent
):
    """A selected-Agent pipeline's deterministic clarification turn, or None.

    The empty-attachment question and the ambiguous / not-found chart-target
    reply stay ahead of the graph (as in Normal RAG) because they only ask the
    user to clarify. File commands are NOT handled here: they run inside the
    Agent's own Step so its tool constraints apply.
    """
    pipeline_metadata = {
        "agent_selection": {
            "agent_id": str(agent.pk),
            "agent_slug": agent.slug,
            "outcome": "pipeline",
        }
    }
    if not question and attachment_ids and not selected:
        return _reply_empty_attachment(
            request, session, attachment_ids, selection_metadata=pipeline_metadata
        )
    if file_commands.classify(question).kind != file_commands.Kind.NONE:
        return None
    return _chart_target_reply(
        request, session, question, attachment_ids,
        selection_metadata=pipeline_metadata,
    )


def _policy_turn(request, session, question, attachment_ids, *, agent, decision):
    """Persist a deterministic selected-Agent policy BLOCK turn (no LLM/tool).

    No file command, control, LangGraph node, or LLM runs. Attachment chips are
    preserved and the blocked provenance is recorded on the assistant turn.
    """
    turn = _chat_service().create_turn(
        session,
        user=session.user,
        question=question,
        response=decision.reply,
        model_name="agent_policy",
        metadata={
            "agent_selection": {
                "agent_id": str(agent.pk),
                "agent_slug": agent.slug,
                "outcome": "blocked",
                "intent": decision.intent,
                "reason": decision.reason,
            }
        },
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
    attachment_ids = list(dict.fromkeys(request.POST.getlist("attachment_ids")))
    # Attachment ids are session entry ids; anything not in this session's
    # file lists is rejected before anything runs.
    try:
        session_files.files_by_ids(session, attachment_ids)
    except session_files.TooManyIds:
        return JsonResponse({"error": "Too many files in one request."}, status=400)
    except session_files.UnknownEntry:
        return JsonResponse({"error": "Unknown attachment."}, status=400)
    # Files checked in a picker; all-or-nothing. Keep this list as
    # ``selected_entry_ids`` (never reuse the name for the model/provider flag).
    selected_entry_ids = list(
        dict.fromkeys(request.POST.getlist("selected_file_ids"))
    )
    try:
        session_files.files_by_ids(session, selected_entry_ids)
    except session_files.TooManyIds:
        return JsonResponse({"error": "Too many files in one request."}, status=400)
    except session_files.UnknownEntry:
        return JsonResponse(
            {"error": "Some selected files are not in this chat."}, status=400
        )

    # A turn needs text, an attachment or a picker selection.
    if not question and not attachment_ids and not selected_entry_ids:
        return JsonResponse(
            {"error": "Type a message or attach a file."}, status=400
        )

    # Resolve the session's selected Agent (repairing a stale FK, carrying its
    # provenance so a repaired selection still records a fallback turn).
    selected_agent, stale_selection_metadata = _resolve_selected_agent(
        session, actor=request.user, repair=True
    )

    resolved_provider, resolved_model = resolve_provider_model(model_name, provider)
    provider_supplied = bool(model_name or provider)
    llm_client = (
        LLMClient(provider=resolved_provider, model=resolved_model)
        if provider_supplied
        else None
    )
    chat_user = session.user
    effective_model = resolved_model if provider_supplied else model_name

    # Capability policy for a selected Agent (before the file-command short
    # circuit, so a selected Agent cannot bypass its own tool restrictions).
    policy_decision = None
    pipeline_entry_ids = []
    if selected_agent is not None:
        from chat.services import agent_capabilities

        policy_decision = agent_capabilities.preflight(
            selected_agent,
            session,
            question,
            attachment_ids,
            selected_entry_ids=selected_entry_ids,
        )
        if policy_decision.outcome == "blocked":
            return _policy_turn(
                request,
                session,
                question,
                attachment_ids,
                agent=selected_agent,
                decision=policy_decision,
            )
        if policy_decision.outcome == "pipeline" and (
            policy_decision.intent == agent_capabilities.Intent.ACTION_PROCESS
        ):
            pipeline_entry_ids = list(selected_entry_ids)

    # Normal RAG and selected-Agent FALLBACK share the file-command short
    # circuit + RAG path. A selected-Agent PIPELINE skips the top-level
    # file-command reply (it would bypass the Agent's tool constraints).
    run_pipeline = (
        selected_agent is not None and policy_decision.outcome == "pipeline"
    )

    fallback_metadata = None
    if selected_agent is not None and policy_decision.outcome == "fallback":
        fallback_metadata = {
            "agent_selection": {
                "agent_id": str(selected_agent.pk),
                "agent_slug": selected_agent.slug,
                "outcome": "fallback",
                "intent": policy_decision.intent,
                "reason": policy_decision.reason,
            }
        }
    # A repaired stale selection on an otherwise Normal-RAG turn carries its own
    # fallback provenance.
    selection_metadata = fallback_metadata or stale_selection_metadata

    if run_pipeline:
        # Only the side-effect-free clarifications run ahead of the graph: they
        # ask the user a question and never act on files or call a tool.
        reply = _pipeline_direct_reply(
            request, session, question, attachment_ids, selected_entry_ids,
            agent=selected_agent,
        )
        if reply is not None:
            return reply
    else:
        reply = _file_command_reply(
            request,
            session,
            question,
            attachment_ids,
            selected_entry_ids,
            selection_metadata=selection_metadata,
        )
        if reply is not None:
            return reply

    try:
        if run_pipeline:
            turn = _orchestrator_for(
                session.company,
                selected_agent=selected_agent,
                llm_client=llm_client,
            ).process_message(
                user=chat_user,
                message_text=question,
                conversation=session,
                company=session.company,
                attachment_ids=attachment_ids or None,
                model_name=effective_model,
                selected_entry_ids=pipeline_entry_ids,
            )
        else:
            turn = _rag_service(llm_client).process_message(
                user=chat_user,
                message_text=question,
                conversation=session,
                company=session.company,
                attachment_ids=attachment_ids or None,
                model_name=effective_model,
                metadata=selection_metadata,
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
            "session": {
                "id": str(session.session_id),
                "title": session.title,
                "selected_agent_id": _selected_agent_id(session),
            },
            "selection_consumed": False,
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
        # purged too. Files attached but never processed are deleted from
        # storage once the delete commits (processed Documents stay).
        from .models import Conversation

        with transaction.atomic():
            keys = session_files.unsettled_keys(session)
            Conversation.all_objects.filter(session=session).hard_delete()
            session.hard_delete()
            transaction.on_commit(partial(session_files.purge_unsettled, keys))

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

    # Only a retry that would start / resume processing needs the selected
    # Agent's control capability; missing / READY / in-flight documents keep
    # the existing no-dispatch response.
    doc_status = (
        Document.objects.filter(pk=document_id, company=session.company)
        .values_list("status", flat=True)
        .first()
    )
    if doc_status is not None and doc_status not in (
        Document.STATUS_READY,
        Document.STATUS_QUEUED,
        Document.STATUS_PROCESSING,
    ):
        from chat.services.agent_capabilities import Intent

        blocked = _require_selected_agent_capability(
            request, session, Intent.ACTION_STOP_RESUME
        )
        if blocked is not None:
            return blocked

    result = _kb.retry_document_processing(
        document_id=document_id, company=session.company, actor=request.user
    )
    if result.get("conflict"):
        status = 409
    else:
        status = 200 if result["ok"] else 502
    return JsonResponse(
        {
            "ok": result["ok"],
            "message": result["content"],
            "workflow_id": result.get("workflow_id"),
            "document_id": document_id,
        },
        status=status,
    )


@login_required
@require_http_methods(["GET"])
def files_json(request, session_id):
    """Live rows for every file of the chat (the picker / tracker panels)."""
    session = _resolve_session(request, session_id)
    rows = [session_files.file_row(f) for f in session_files.reconcile(session)]
    return JsonResponse(
        {"files": rows, "all_terminal": all(row["terminal"] for row in rows)}
    )


@login_required
@require_http_methods(["POST"])
def panel_process(request, session_id):
    """Process picker selections without creating a chat conversation turn."""
    session = _resolve_session(request, session_id)
    from chat.services.agent_capabilities import Intent

    blocked = _require_selected_agent_capability(
        request, session, Intent.ACTION_PROCESS
    )
    if blocked is not None:
        return blocked
    entry_ids = list(dict.fromkeys(request.POST.getlist("entry_ids")))
    if not entry_ids:
        return JsonResponse({"error": "Select at least one file to process."}, status=400)
    try:
        session_files.files_by_ids(session, entry_ids)
    except session_files.TooManyIds:
        return JsonResponse({"error": "Too many files in one request."}, status=400)
    except session_files.UnknownEntry:
        return JsonResponse({"error": "Some selected files are not in this chat."}, status=400)

    try:
        file_commands.process_entries(session, entry_ids, request.user)
    except Exception:  # noqa: BLE001 - keep the picker mounted for retry
        logger.exception("Could not process panel files in session %s", session.pk)
        return JsonResponse({"error": file_commands.START_FAILED}, status=502)

    rows = [
        session_files.file_row(file)
        for file in session_files.files_by_ids(session, entry_ids)
    ]
    return JsonResponse({"files": rows}, status=202)


_FILE_ACTIONS = {"stop": "stop", "resume": "resume", "retry": "resume"}


def _one_file(session, entry_id):
    try:
        found = session_files.files_by_ids(session, [entry_id])
    except session_files.UnknownEntry:
        return None
    return found[0] if found else None


@login_required
@require_http_methods(["POST"])
def file_action(request, session_id, entry_id, action):
    """Stop (200) or resume (202) one chat file; 409 when its state disallows it."""
    session = _resolve_session(request, session_id)
    action = _FILE_ACTIONS.get(action)
    if action is None:
        return JsonResponse({"error": "Unknown action."}, status=404)
    from chat.services.agent_capabilities import Intent

    blocked = _require_selected_agent_capability(
        request, session, Intent.ACTION_STOP_RESUME
    )
    if blocked is not None:
        return blocked
    session_files.reconcile(session)
    target = _one_file(session, entry_id)
    if target is None:
        return JsonResponse({"error": "Unknown file."}, status=404)

    def _conflict(message, current=target):
        return JsonResponse(
            {"ok": False, "error": message, "file": session_files.file_row(current)},
            status=409,
        )

    title = f"“{target.title}”"
    if target.state == "not_started":
        return _conflict(f"{title} hasn't been processed yet. Select it and press Process.")
    if target.state == "removed":
        return _conflict(f"{title} was removed from Documents.")

    try:
        if action == "stop":
            # Outside the session lock: terminate is network IO.
            result = controls.stop_document(target.document)
            status = 200
        else:
            with session_files.locked_files(session, actor=request.user):
                document = (
                    Document.objects.select_for_update()
                    .filter(pk=target.document_id, company=session.company_id)
                    .first()
                )
                if document is None:
                    raise controls.ControlError(f"{title} was removed from Documents.")
                result = controls.resume_document(document)
            status = 202
    except controls.ControlError as exc:
        return _conflict(exc.message, _one_file(session, entry_id) or target)
    except Exception:  # noqa: BLE001 - report a retryable failure
        logger.exception("Chat file %s %s failed in session %s", entry_id, action, session.pk)
        return JsonResponse(
            {"ok": False, "error": f"Couldn't {action} {title}. Please try again."},
            status=502,
        )

    current = _one_file(session, entry_id) or target
    return JsonResponse(
        {"ok": True, "message": result.message, "file": session_files.file_row(current)},
        status=status,
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
    """Store an uploaded file as an unprocessed entry on the session.

    The bytes go under ``media/unsettled/`` and an entry is appended to
    ``session.context["unprocessed_docs"]``. No Document row is created until
    the user processes the file. The returned ``id`` is the entry id.
    """
    session = _resolve_session(request, session_id)

    try:
        entry = session_files.store_upload(
            session, request.FILES.get("file"), request.user
        )
    except session_files.UploadRejected as exc:
        return JsonResponse({"error": exc.message}, status=exc.status)
    except Exception:
        logger.exception("Could not attach a file to chat session %s", session.pk)
        return JsonResponse({"error": "Could not attach the file."}, status=500)

    return JsonResponse(
        {
            "id": entry["id"],
            "title": entry["title"],
            "file_type": entry["file_type"],
        },
        status=201,
    )
