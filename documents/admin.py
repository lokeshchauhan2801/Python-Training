"""Admin registration for the Documents app.

``DocumentAdmin`` / ``DocumentChunkAdmin`` use the reusable ``BaseModelAdmin``
(Unfold ``ModelAdmin`` subclass → the Unfold shell/left sidebar/company switcher
are preserved on every page, plus soft-delete visibility, owner scoping, and
audit stamping).

HOUSE STYLE (the STANDARD changelist pattern to reuse for every app admin —
scrapper, chat, company — so the admin looks uniform across services):
  * ``date_hierarchy = "created_at"`` → the date drill bar across the top.
  * ``search_fields`` → the "Type to search" bar.
  * ``list_filter`` → the "Filters" button (top-right).
  * a short ID column, then the domain columns, a colored status badge via a
    display method, and ``created_by`` / ``created_at`` trailing columns.
  * Unfold ``actions_row`` PILL buttons (purple = ``ActionVariant.PRIMARY``) for
    per-row operations, each ``@action``-decorated and operating ONLY on the
    scoped ``self.get_queryset(request)``, with POST paths CSRF-validated.

A library ``Document`` added/changed in the admin is extracted synchronously in
``save_model`` so its chunks appear immediately (Phase 1 — no async broker).
"""

from django.contrib import admin, messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.html import format_html
from unfold.decorators import action
from unfold.enums import ActionVariant

from basics.admin import BaseModelAdmin

from .models import Document, DocumentChunk
from .services import DocumentIndexError, DocumentValidationError, process_document

_STATUS_COLORS = {
    Document.STATUS_READY: "#16a34a",  # green
    Document.STATUS_FAILED: "#dc2626",  # red
    Document.STATUS_PROCESSING: "#d97706",  # amber
    Document.STATUS_QUEUED: "#d97706",  # amber
    Document.STATUS_UPLOADED: "#6b7280",  # grey
}


@admin.register(Document)
class DocumentAdmin(BaseModelAdmin):
    # Per-row purple pills (ActionVariant.PRIMARY). All resolve the row from the
    # scoped queryset; "process" re-runs synchronous extraction, "show" opens
    # the scoped chunk view, "schedule" is a Phase-4 placeholder (no-op).
    actions_row = ("process_now", "show_chunks", "schedule_processing")

    list_display = (
        "id_short",
        "title",
        "file_type",
        "status_badge",
        "chunk_count",
        "processing_backend",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "file_type", "source", "company", "is_deleted", "created_at")
    search_fields = ("title", "uploaded_by__email")
    date_hierarchy = "created_at"

    fieldsets = (
        (None, {"fields": ("id", "title", "source", "file", "file_type")}),
        (
            "Processing",
            {
                "fields": (
                    "status",
                    "processing_backend",
                    "processing_stage",
                    "processing_progress",
                    "processing_error",
                    "chunk_count",
                    "processing_attempts",
                    "processing_duration_ms",
                    "file_size",
                    "content_type",
                )
            },
        ),
        ("Tenant", {"fields": ("company", "uploaded_by", "session")}),
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

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        for name in (
            "status",
            "processing_backend",
            "processing_stage",
            "processing_progress",
            "processing_task_id",
            "processing_error",
            "chunk_count",
            "processing_attempts",
            "processing_duration_ms",
            "file_size",
            "content_type",
        ):
            if name not in readonly:
                readonly.append(name)
        return readonly

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        color = _STATUS_COLORS.get(obj.status, "#6b7280")
        return format_html(
            '<span style="color:{}; font-weight:600;">{}</span>',
            color,
            obj.get_status_display(),
        )

    # --- per-row pill actions ----------------------------------------------
    @action(
        description="Process now",
        url_path="process-now",
        variant=ActionVariant.PRIMARY,
    )
    def process_now(self, request, object_id):
        document = get_object_or_404(self.get_queryset(request), pk=object_id)
        change_url = reverse("admin:documents_document_change", args=[document.pk])
        if document.source != Document.SOURCE_LIBRARY:
            self.message_user(
                request,
                "Only library documents can be processed.",
                level=messages.WARNING,
            )
            return redirect(change_url)
        try:
            process_document(document)
        except (DocumentValidationError, DocumentIndexError) as exc:
            self.message_user(
                request,
                f"Extraction failed for “{document.title}”: {exc}",
                level=messages.WARNING,
            )
        else:
            self.message_user(
                request,
                f"Extracted {document.chunk_count} chunk(s) from “{document.title}”.",
                level=messages.SUCCESS,
            )
        return redirect(change_url)

    @action(
        description="Show",
        url_path="show-chunks",
        variant=ActionVariant.PRIMARY,
    )
    def show_chunks(self, request, object_id):
        document = get_object_or_404(self.get_queryset(request), pk=object_id)
        return redirect(
            reverse(
                "admin:documents_document_chunks_json", args=[document.pk]
            )
        )

    @action(
        description="Schedule",
        url_path="schedule",
        variant=ActionVariant.PRIMARY,
    )
    def schedule_processing(self, request, object_id):
        # PHASE 4 PLACEHOLDER: async scheduling (Celery/Kafka/Temporal) is not
        # implemented in Phase 1. This pill is intentionally a no-op so the row
        # layout matches the reference; it will dispatch to a processing backend
        # in Phase 4.
        document = get_object_or_404(self.get_queryset(request), pk=object_id)
        self.message_user(
            request,
            "Scheduled background processing is coming in a later phase.",
            level=messages.INFO,
        )
        return redirect(
            reverse("admin:documents_document_change", args=[document.pk])
        )

    def save_model(self, request, obj, form, change):
        if not change and not obj.uploaded_by_id:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)
        # A library document is extracted synchronously on save so its chunks
        # appear immediately (Phase 1 — no async broker). Chat attachments are
        # NOT auto-processed (Phase 3 wires that flow).
        if obj.source == Document.SOURCE_LIBRARY:
            try:
                process_document(obj)
            except (DocumentValidationError, DocumentIndexError) as exc:
                # The service already persisted status=failed with the message.
                self.message_user(
                    request,
                    f"Extraction failed for “{obj.title}”: {exc}",
                    level=messages.WARNING,
                )
            else:
                self.message_user(
                    request,
                    f"Extracted {obj.chunk_count} chunk(s) from “{obj.title}”.",
                    level=messages.SUCCESS,
                )

    def get_urls(self):
        # Custom URLs BEFORE super().get_urls() so they take precedence.
        custom = [
            path(
                "<str:pk>/chunks-json/",
                self.admin_site.admin_view(self.chunks_json_view),
                name="documents_document_chunks_json",
            ),
        ]
        return custom + super().get_urls()

    def chunks_json_view(self, request, pk):
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        # Resolve from the permission-scoped queryset ONLY (never all_objects):
        # out-of-scope documents 404.
        document = get_object_or_404(self.get_queryset(request), pk=pk)
        chunks = (
            DocumentChunk.objects.filter(document=document)
            .order_by("chunk_index")
            .values("id", "chunk_index", "page_number", "source_location", "content")
        )
        return JsonResponse(
            {
                "document": {
                    "id": str(document.pk),
                    "title": document.title,
                    "source": document.source,
                    "status": document.status,
                    "chunk_count": document.chunk_count,
                },
                "chunks": [
                    {
                        "id": str(chunk["id"]),
                        "chunk_index": chunk["chunk_index"],
                        "page_number": chunk["page_number"],
                        "source_location": chunk["source_location"],
                        "content": chunk["content"],
                    }
                    for chunk in chunks
                ],
            }
        )


@admin.register(DocumentChunk)
class DocumentChunkAdmin(BaseModelAdmin):
    # House style, read-only. Columns match the OLD "Document chunks" changelist:
    # ID · Document (parent title) · Index · Text (preview) · Embedding · Created.
    list_display = (
        "id_short",
        "document",
        "chunk_index",
        "text_preview",
        "embedding_preview",
        "created_at",
    )
    list_filter = ("document", "page_number")
    search_fields = ("document__title", "content")
    list_display_links = ("id_short",)

    @admin.display(description="ID")
    def id_short(self, obj):
        return str(obj.pk)[:8]

    @admin.display(description="Index", ordering="chunk_index")
    def chunk_index_display(self, obj):  # pragma: no cover - alias kept simple
        return obj.chunk_index

    @admin.display(description="Text")
    def text_preview(self, obj):
        text = obj.content or ""
        return text[:120] + ("…" if len(text) > 120 else "")

    @admin.display(description="Embedding")
    def embedding_preview(self, obj):
        # PHASE 1: no embeddings exist yet. In OLD the vector preview is read
        # back (superuser-only) from ChromaDB via ChromaVectorStore.get_embeddings
        # — it is NOT a field on the row. PHASE 2 will wire this column to the
        # real truncated vector preview ("[-0.05, 0.03, ...] +N more"). Until
        # then every row shows a clear placeholder so the layout matches.
        return "Not embedded (Phase 2)"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
