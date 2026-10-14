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

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path

from basics.admin import BaseModelAdmin
from basics.llm import LLMConfigurationError

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

    # --- Goal 2: custom URLs (BEFORE super().get_urls()) -------------------
    def get_urls(self):
        custom = [
            path(
                "<uuid:pk>/detail-json/",
                self.admin_site.admin_view(self.detail_json_view),
                name="chat_conversationsession_detail_json",
            ),
            path(
                "<uuid:pk>/summarize/",
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
            summary = ChatService().summarize(session=session, actor=request.user)
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
