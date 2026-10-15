"""Admin for the chat domain, demonstrating ALL FIVE Unfold learning goals.

Goal 1 - app-local ``change_list_template`` / ``change_form_template`` that
         extend the Unfold templates and add buttons.
Goal 2 - ``get_urls()`` adds CSRF/permission-protected AJAX endpoints
         (``detail-json`` GET JSON, ``summarize`` POST CSRF) plus a ``learning``
         page, all wrapped in ``admin_view`` and registered BEFORE super().
Goal 3 - ``learning_view`` returns a ``TemplateResponse`` with admin context.
Goal 4 - full admin config (list_display/filter/search/date_hierarchy/fieldsets/
         readonly + a ``ConversationInline`` + display methods).
Goal 5 - Unfold ``actions_row`` / ``actions_detail`` buttons wired to the AJAX
         endpoints + bulk soft-delete/restore, rendered by the overridden
         templates.

Security: every custom view is wrapped in ``admin_view`` (login + permission),
resolves objects from the permission-scoped ``self.get_queryset(request)`` ONLY,
answers AJAX GET with JSON and validates CSRF on POST, and the client JS renders
server data with ``textContent`` / ``createElement`` / ``appendChild`` (NEVER
``innerHTML``) so injected content can never execute. AJAX updates the page
WITHOUT a full refresh.
"""

import json

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path

from basics.admin import BaseModelAdmin
from basics.llm import LLMBackendError, LLMConfigurationError

from company.models import Company

from .models import Conversation, ConversationSession
from .services import ChatService


class ConversationInline(admin.TabularInline):
    model = Conversation
    extra = 0
    fields = ("question", "response", "model_name", "created_at")
    readonly_fields = ("created_at",)
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        # Turns are created through the chat service, not hand-added here.
        return False


@admin.register(ConversationSession)
class ConversationSessionAdmin(BaseModelAdmin):
    # Goal 1: app-local templates extending the Unfold admin templates.
    change_list_template = "admin/chat/conversationsession/change_list.html"
    change_form_template = "admin/chat/conversationsession/change_form.html"

    # Goal 4: full config.
    list_display = (
        "title",
        "session_id",
        "user",
        "company",
        "model_name",
        "turn_count",
        "created_at",
        "is_deleted",
    )
    list_filter = ("company", "model_name", "is_deleted", "created_at")
    search_fields = ("title", "session_id", "user__email")
    date_hierarchy = "created_at"
    inlines = [ConversationInline]

    fieldsets = (
        (None, {"fields": ("id", "session_id", "title", "user", "company")}),
        ("Content", {"fields": ("content", "model_name", "summary")}),
        ("Meta", {"fields": ("context", "media", "metadata")}),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "created_by",
                    "updated_by",
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
    )
    # session_id is read-only in addition to the audit fields appended by the
    # AuditAdminMixin.
    readonly_fields = ("session_id",)

    # Goal 5: bulk soft-delete/restore come from BaseModelAdmin.actions; the
    # per-session "Detail JSON" / "Summarize" buttons are rendered by the
    # overridden change_form template and call the AJAX endpoints below.

    # --- display methods (Goal 4) ------------------------------------------
    @admin.display(description="Turns")
    def turn_count(self, obj):
        return obj.turns.count()

    @admin.display(description="Summary")
    def short_summary(self, obj):
        if not obj.summary:
            return "-"
        return (obj.summary[:80] + "...") if len(obj.summary) > 80 else obj.summary

    # --- LLM-client seam so tests can inject a fake client -----------------
    def _chat_service(self):
        """Build the chat service (default real, lazy, no key at construct).

        A single seam the HTTP-layer tests patch to inject a fake LLM client.
        """
        return ChatService()

    # --- scope helpers (re-derive scope server-side, never trust client) ---
    def _allowed_companies(self, request):
        """Companies the requesting user may use (non-deleted only)."""
        if request.user.is_superuser:
            return Company.objects.all()
        return Company.objects.filter(owner=request.user)

    def _scoped_sessions(self, request):
        """Non-deleted sessions owned by / visible to the requesting user.

        Uses the non-deleted ``objects`` manager (user-facing list), NOT the
        changelist's soft-delete-inclusive queryset. Superusers see all
        non-deleted sessions; everyone else only their own, limited to
        companies they own.
        """
        qs = ConversationSession.objects.all()
        if not request.user.is_superuser:
            qs = qs.filter(
                user=request.user,
                company__in=self._allowed_companies(request),
            )
        return qs.order_by("-created_at")

    # --- Goal 2: custom URLs (BEFORE super().get_urls()) -------------------
    def get_urls(self):
        admin_view = self.admin_site.admin_view
        custom = [
            path(
                "chat/",
                admin_view(self.chat_page_view),
                name="chat_conversation_sessions",
            ),
            path(
                "chat/new/",
                admin_view(self.new_session_view),
                name="chat_conversation_new",
            ),
            path(
                "chat/companies-json/",
                admin_view(self.companies_json_view),
                name="chat_conversation_companies_json",
            ),
            path(
                "chat/sessions-json/",
                admin_view(self.sessions_json_view),
                name="chat_conversation_sessions_json",
            ),
            path(
                "chat/<str:pk>/turns-json/",
                admin_view(self.session_turns_json_view),
                name="chat_conversation_turns_json",
            ),
            path(
                "chat/<str:pk>/send/",
                admin_view(self.send_message_view),
                name="chat_conversation_send",
            ),
            path(
                "<str:pk>/detail-json/",
                self.admin_site.admin_view(self.detail_json_view),
                name="chat_conversationsession_detail_json",
            ),
            path(
                "<str:pk>/summarize/",
                self.admin_site.admin_view(self.summarize_view),
                name="chat_conversationsession_summarize",
            ),
            path(
                "learning/",
                self.admin_site.admin_view(self.learning_view),
                name="chat_conversationsession_learning",
            ),
        ]
        return custom + super().get_urls()

    # --- Goal 2: AJAX GET -> JSON ------------------------------------------
    def detail_json_view(self, request, pk):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        # Permission-scoped resolution ONLY.
        session = get_object_or_404(self.get_queryset(request), pk=pk)
        turns = [
            {
                "id": str(turn.id),
                "question": turn.question,
                "response": turn.response,
                "model_name": turn.model_name,
                "created_at": turn.created_at.isoformat(),
            }
            for turn in session.turns.order_by("created_at")
        ]
        return JsonResponse(
            {
                "id": str(session.id),
                "session_id": session.session_id,
                "title": session.title,
                "model_name": session.model_name,
                "summary": session.summary,
                "turns": turns,
            }
        )

    # --- Goal 2: AJAX POST (CSRF-validated, NOT csrf_exempt) ---------------
    def summarize_view(self, request, pk):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        session = get_object_or_404(self.get_queryset(request), pk=pk)
        try:
            summary = self._chat_service().summarize(
                session=session, actor=request.user
            )
        except LLMConfigurationError:
            return JsonResponse({"error": "LLM not configured"}, status=503)
        return JsonResponse({"summary": summary})

    # --- Goal 3: custom learning page via TemplateResponse -----------------
    def learning_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Chat learning map",
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request, "admin/chat/conversationsession/learning_view.html", context
        )

    # --- Chat UI: page view (TemplateResponse) -----------------------------
    def chat_page_view(self, request):
        """The three-area chat page. No server data embedded; JS fetches it."""
        context = {
            **self.admin_site.each_context(request),
            "title": "Chat",
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request, "admin/chat/conversationsession/chat_page.html", context
        )

    # --- Chat UI: companies-list JSON (scoped) -----------------------------
    def companies_json_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        companies = [
            {"id": str(company.id), "name": company.name}
            for company in self._allowed_companies(request).order_by("name")
        ]
        return JsonResponse({"companies": companies})

    # --- Chat UI: sessions-list JSON (scoped, filterable by company) -------
    def sessions_json_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        sessions = self._scoped_sessions(request)

        company_id = request.GET.get("company")
        if company_id:
            # Re-validate the client-supplied company against scope; an
            # out-of-scope id yields an empty list (never leaks other data).
            allowed = self._allowed_companies(request).filter(pk=company_id)
            if not allowed.exists():
                return JsonResponse({"sessions": []})
            sessions = sessions.filter(company_id=company_id)

        payload = []
        for session in sessions:
            hint = session.summary or ""
            if not hint:
                last = session.turns.order_by("-created_at").first()
                if last is not None:
                    hint = last.question
            if len(hint) > 80:
                hint = hint[:80] + "..."
            payload.append(
                {
                    "id": str(session.id),
                    "session_id": session.session_id,
                    "title": session.title,
                    "hint": hint,
                }
            )
        return JsonResponse({"sessions": payload})

    # --- Chat UI: session-turns JSON (scoped, 404 outside scope) -----------
    def session_turns_json_view(self, request, pk):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        session = get_object_or_404(self._scoped_sessions(request), pk=pk)
        turns = [
            {
                "id": str(turn.id),
                "question": turn.question,
                "response": turn.response,
                "model_name": turn.model_name,
                "created_at": turn.created_at.isoformat(),
            }
            for turn in session.turns.order_by("created_at")
        ]
        return JsonResponse(
            {
                "id": str(session.id),
                "session_id": session.session_id,
                "title": session.title,
                "company_id": str(session.company_id),
                "turns": turns,
            }
        )

    # --- Chat UI: create a new session under a scoped company --------------
    def new_session_view(self, request):
        # Always answer with JSON (never a redirect) so the AJAX client can
        # create a session strictly in-place with no full-page navigation.
        if request.method not in ("GET", "POST"):
            return JsonResponse({"error": "Method not allowed"}, status=405)

        source = request.POST if request.method == "POST" else request.GET
        company_id = source.get("company")

        allowed = self._allowed_companies(request)
        if not allowed.exists():
            return JsonResponse({"error": "No company available."}, status=400)

        if company_id:
            # Re-validate ownership server-side; out-of-scope => 404 (no leak).
            company = get_object_or_404(allowed, pk=company_id)
        else:
            company = allowed.order_by("name").first()

        session = ConversationSession(
            company=company, user=request.user, title="New chat"
        )
        session.save(actor=request.user)

        return JsonResponse(
            {
                "id": str(session.id),
                "session_id": session.session_id,
                "title": session.title,
            },
            status=201,
        )

    # --- Chat UI: send a message (POST, CSRF-validated like summarize) -----
    def send_message_view(self, request, pk):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        session = get_object_or_404(self._scoped_sessions(request), pk=pk)

        question = request.POST.get("question")
        if question is None and request.body:
            # Support a JSON body as well as form-encoded.
            try:
                question = (json.loads(request.body) or {}).get("question")
            except (ValueError, TypeError):
                question = None
        question = (question or "").strip()
        if not question:
            return JsonResponse({"error": "Question is required."}, status=400)

        try:
            turn = self._chat_service().ask(
                session=session, user=request.user, question=question
            )
        except LLMConfigurationError:
            return JsonResponse({"error": "LLM not configured"}, status=503)
        except LLMBackendError:
            return JsonResponse({"error": "LLM request failed"}, status=502)

        return JsonResponse(
            {
                "turn": {
                    "id": str(turn.id),
                    "question": turn.question,
                    "response": turn.response,
                    "model_name": turn.model_name,
                    "created_at": turn.created_at.isoformat(),
                }
            },
            status=201,
        )


@admin.register(Conversation)
class ConversationAdmin(BaseModelAdmin):
    list_display = (
        "session",
        "user",
        "model_name",
        "short_question",
        "created_at",
        "is_deleted",
    )
    list_filter = ("company", "model_name", "is_deleted", "created_at")
    search_fields = ("question", "response", "session__session_id")
    date_hierarchy = "created_at"

    fieldsets = (
        (None, {"fields": ("id", "session", "user", "company", "model_name")}),
        ("Turn", {"fields": ("question", "response")}),
        ("Meta", {"fields": ("context", "media", "metadata")}),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "created_by",
                    "updated_by",
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
    )

    @admin.display(description="Question")
    def short_question(self, obj):
        return (obj.question[:60] + "...") if len(obj.question) > 60 else obj.question
