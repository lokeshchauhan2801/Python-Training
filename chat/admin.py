import json

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path

from basics.admin import BaseModelAdmin
from basics.llm import LLMBackendError, LLMConfigurationError

from .models import Conversation, ConversationSession
from .scope import allowed_companies as _allowed_companies_for
from .scope import scoped_sessions as _scoped_sessions_for
from .services import ChatService


class SessionTurnInline(admin.TabularInline):
    """The actual chat of a session, read from the ``Conversation`` turns.

    A turn is a (question, response) pair; this inline shows the real turns at
    the bottom of the session form, newest first, read-only."""

    model = Conversation
    fk_name = "session"
    extra = 0
    fields = ("question", "response", "model_name", "created_at")
    readonly_fields = ("question", "response", "model_name", "created_at")
    # Newest turn first (most recent at the top).
    ordering = ("-created_at",)
    show_change_link = False
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConversationSession)
class ConversationSessionAdmin(BaseModelAdmin):

    change_list_template = "admin/chat/conversationsession/change_list.html"
    change_form_template = "admin/chat/conversationsession/change_form.html"


    list_display = (
        "title",
        "session_id",
        "user",
        "company",
        "turn_count",
        "created_at",
        "is_deleted",
    )
    list_filter = ("company", "is_deleted", "created_at")
    search_fields = ("title", "session_id", "user__email")
    date_hierarchy = "created_at"
    inlines = [SessionTurnInline]

    # The ULID ``session_id`` is the single session identifier (shown
    # read-only). ``metadata`` is kept (read-only) because it carries state such
    # as the pending KB file selection; ``context`` is not user-edited for a
    # session. The whole chat shows in the SessionTurnInline at the bottom.
    fieldsets = (
        (None, {"fields": ("session_id", "title", "user", "company")}),
        ("Content", {"fields": ("summary", "metadata")}),
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

    readonly_fields = ("session_id", "metadata")

    def get_readonly_fields(self, request, obj=None):
        """A session's owner (``user``) and ``company`` are fixed once created.

        Reassigning either would move an existing conversation to a different
        user/company, which is illogical and a data-integrity/scoping hazard.
        They remain editable only when adding a new session; on an existing
        session they are read-only.
        """
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            for name in ("user", "company"):
                if name not in readonly:
                    readonly.append(name)
        return readonly


    @admin.display(description="Turns")
    def turn_count(self, obj):
        return obj.turns.count()

    @admin.display(description="Summary")
    def short_summary(self, obj):
        if not obj.summary:
            return "-"
        return (obj.summary[:80] + "...") if len(obj.summary) > 80 else obj.summary


    def _chat_service(self):
        """Build the chat service (default real, lazy, no key at construct).

        A single seam the HTTP-layer tests patch to inject a fake LLM client.
        """
        return ChatService()


    def _allowed_companies(self, request):
        """Companies the requesting user may use (shared with /chat/ views)."""
        return _allowed_companies_for(request)

    def _scoped_sessions(self, request):
        """Non-deleted sessions visible to the user (shared with /chat/ views)."""
        return _scoped_sessions_for(request)


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


    def detail_json_view(self, request, pk):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)

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
                "id": str(session.session_id),
                "title": session.title,
                "summary": session.summary,
                "turns": turns,
            }
        )


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


    def learning_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Chat learning map",
            "opts": self.model._meta,
        }
        return TemplateResponse(
            request, "admin/chat/conversationsession/learning_view.html", context
        )


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


    def companies_json_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        companies = [
            {"id": str(company.id), "name": company.name}
            for company in self._allowed_companies(request).order_by("name")
        ]
        return JsonResponse({"companies": companies})


    def sessions_json_view(self, request):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        sessions = self._scoped_sessions(request)

        company_id = request.GET.get("company")
        if company_id:


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
                    "id": str(session.session_id),
                    "title": session.title,
                    "hint": hint,
                }
            )
        return JsonResponse({"sessions": payload})


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
                "id": str(session.session_id),
                "title": session.title,
                "company_id": str(session.company_id),
                "turns": turns,
            }
        )


    def new_session_view(self, request):


        if request.method not in ("GET", "POST"):
            return JsonResponse({"error": "Method not allowed"}, status=405)

        source = request.POST if request.method == "POST" else request.GET
        company_id = source.get("company")

        allowed = self._allowed_companies(request)
        if not allowed.exists():
            return JsonResponse({"error": "No company available."}, status=400)

        if company_id:

            company = get_object_or_404(allowed, pk=company_id)
        else:
            company = allowed.order_by("name").first()

        session = ConversationSession(
            company=company, user=request.user, title="New chat"
        )
        session.save(actor=request.user)

        return JsonResponse(
            {
                "id": str(session.session_id),
                "title": session.title,
            },
            status=201,
        )


    def send_message_view(self, request, pk):
        if request.method != "POST":
            return JsonResponse({"error": "Method not allowed"}, status=405)
        session = get_object_or_404(self._scoped_sessions(request), pk=pk)

        question = request.POST.get("question")
        if question is None and request.body:

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
        ("Meta", {"fields": ("context", "metadata")}),
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
