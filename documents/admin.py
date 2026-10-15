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
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from unfold.decorators import action
from unfold.enums import ActionVariant

from basics.admin import BaseModelAdmin
from basics.vectorstore import ChromaVectorStore

from .forms import DocumentAdminForm
from .models import Document, DocumentChunk
from .services import VECTOR_COLLECTION
from .tasks import dispatch_document_processing

# The four options the single "Schedule" dropdown/chooser offers, in display
# order. Each maps to how the existing dispatcher is invoked:
#   * "now"      → dispatch via the document's current/default backend (the old
#                  "Process now": DEBUG celery inline / synchronous path).
#   * "celery"   → set processing_backend='celery' and dispatch via celery.
#   * "kafka"    → set processing_backend='kafka' and dispatch via kafka.
#   * "temporal" → set processing_backend='temporal' and dispatch via temporal.
_SCHEDULE_CHOICES = (
    ("now", "Process now"),
    ("celery", "Process with Celery"),
    ("kafka", "Process with Kafka"),
    ("temporal", "Process with Temporal"),
)
# How recently a document must have been updated to appear in the status poll.
_STATUS_POLL_WINDOW_SECONDS = 600

def _embedding_preview_text(vector):
    """Render a vector as the screenshot's truncated preview, or '—' when empty.

    Example: ``[-0.0515, 0.0351, -0.1768, 0.0042] … +764 more`` (first 4 dims
    rounded to 4 places, then ``+N more`` where N = total_dims - 4). A missing or
    empty vector renders as ``—``.
    """
    if not vector:
        return "—"
    head = ", ".join(f"{round(float(value), 4)}" for value in vector[:4])
    remaining = len(vector) - 4
    if remaining > 0:
        return f"[{head}] … +{remaining} more"
    return f"[{head}]"


_STATUS_COLORS = {
    Document.STATUS_READY: "#16a34a",  # green
    Document.STATUS_FAILED: "#dc2626",  # red
    Document.STATUS_PROCESSING: "#d97706",  # amber
    Document.STATUS_QUEUED: "#d97706",  # amber
    Document.STATUS_UPLOADED: "#6b7280",  # grey
}


@admin.register(Document)
class DocumentAdmin(BaseModelAdmin):
    # HOUSE RULE: the uploader is auto-set to request.user and read-only (never
    # a selectable dropdown) via BaseModelAdmin.actor_field; the company FK is
    # scoped/defaulted to the user's owned company by CompanyScopedAdminMixin.
    actor_field = "uploaded_by"

    # Add/change form surfaces the file-OR-URL rule as a field error.
    form = DocumentAdminForm

    # Changelist override adds the completion-notification status poller while
    # keeping the house-style/Unfold changelist intact.
    change_list_template = "admin/documents/document/change_list.html"

    # Per-row actions (ActionVariant.PRIMARY). All resolve the row from the
    # scoped queryset. "Schedule" opens a single CSRF-protected chooser that
    # expands to the four processing options (Process now / with Celery /
    # with Kafka / with Temporal), each wired to the existing dispatcher;
    # "Show" opens the scoped chunk view.
    actions_row = ("schedule", "show_chunks")

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
        (
            None,
            {
                "fields": ("id", "title", "source", "file", "source_url", "file_type"),
                "description": (
                    "For a library document, upload a file OR paste a media URL "
                    "(exactly one). A URL is downloaded in the background by the "
                    "scrapper, then processed with the selected backend."
                ),
            },
        ),
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

    # --- per-row actions ----------------------------------------------------
    @action(
        description="Schedule",
        url_path="schedule",
        variant=ActionVariant.PRIMARY,
    )
    def schedule(self, request, object_id):
        # The single "Schedule" row control. GET renders a CSRF-protected chooser
        # page whose four buttons expand the control to {Process now, Process
        # with Celery, Process with Kafka, Process with Temporal}. POST reads the
        # chosen option and dispatches via the EXISTING dispatcher seam:
        #   * "now"      → dispatch via the document's current/default backend
        #                  (DEBUG celery inline / synchronous path), exactly as
        #                  the old "Process now".
        #   * celery/kafka/temporal → record processing_backend and dispatch via
        #                  that backend.
        # Resolved from the scoped queryset ONLY (out-of-scope → 404); POST is
        # CSRF-validated by the admin_view wrapper + the template's
        # {% csrf_token %}.
        document = get_object_or_404(self.get_queryset(request), pk=object_id)
        change_url = reverse("admin:documents_document_change", args=[document.pk])
        if document.source != Document.SOURCE_LIBRARY:
            self.message_user(
                request,
                "Only library documents can be processed.",
                level=messages.WARNING,
            )
            return redirect(change_url)

        if request.method == "POST":
            choice = request.POST.get("schedule", "")
            valid = {value for value, _label in _SCHEDULE_CHOICES}
            if choice not in valid:
                self.message_user(
                    request,
                    "Pick a valid option (Process now, Celery, Kafka, or "
                    "Temporal).",
                    level=messages.WARNING,
                )
                return redirect(request.path)
            # "Process now" dispatches via the document's current/default backend
            # (no explicit backend); the other three set the backend explicitly.
            backend = None if choice == "now" else choice
            if backend is not None:
                document.processing_backend = backend
            self._reset_for_reprocess(document)
            _job_id, resolved = dispatch_document_processing(
                str(document.pk), backend=backend
            )
            self.message_user(
                request,
                f"Queued “{document.title}” for processing via {resolved}.",
                level=messages.INFO,
            )
            return redirect(change_url)

        context = {
            **self.admin_site.each_context(request),
            "title": f"Schedule — {document.title}",
            "opts": self.model._meta,
            "original": document,
            "document": document,
            "schedule_options": _SCHEDULE_CHOICES,
            "change_url": change_url,
        }
        return TemplateResponse(
            request,
            "admin/documents/document/schedule.html",
            context,
        )

    def _reset_for_reprocess(self, document):
        """Clear prior chunks/vectors + processing state before a re-dispatch.

        Hard-deletes the document's chunks (incl. soft-deleted) and zeroes the
        progress/attempts/error so the backend starts from a clean state. The
        QUEUED transition itself is set by ``dispatch_document_processing``.
        """
        DocumentChunk.all_objects.filter(document=document).hard_delete()
        document.chunk_count = 0
        document.processing_attempts = 0
        document.processing_progress = 0
        document.processing_error = ""
        document.save(
            update_fields=[
                "chunk_count",
                "processing_attempts",
                "processing_progress",
                "processing_error",
                # Persist a caller-set processing_backend (e.g. the "Process
                # with…" chooser) so the choice survives even if the dispatch
                # itself is a no-op for this backend.
                "processing_backend",
                "updated_at",
            ]
        )

    @action(
        description="Show",
        url_path="show-chunks",
        variant=ActionVariant.PRIMARY,
    )
    def show_chunks(self, request, object_id):
        # Redirect to the Unfold-shelled HTML viewer (sidebar intact), NOT the
        # raw JSON endpoint. The HTML page fetches the JSON as its data source.
        document = get_object_or_404(self.get_queryset(request), pk=object_id)
        return redirect(
            reverse("admin:documents_document_chunks", args=[document.pk])
        )

    def save_model(self, request, obj, form, change):
        # BaseModelAdmin stamps the actor (uploaded_by=request.user) + enforces
        # the owned company server-side; this override then dispatches library
        # processing through the backend seam.
        super().save_model(request, obj, form, change)
        # A library document is dispatched for processing on save (Phase 4). In
        # DEBUG the default celery path runs inline on a background thread so
        # chunks appear almost immediately and the status poll surfaces the
        # completion notification; a URL-sourced document is downloaded first on
        # that same background path. Kafka/Temporal dispatch and return
        # immediately (Queued) for their worker to pick up. Chat attachments are
        # NOT auto-processed (Phase 3 wires that flow).
        if obj.source == Document.SOURCE_LIBRARY:
            _job_id, resolved = dispatch_document_processing(str(obj.pk))
            self.message_user(
                request,
                f"Queued “{obj.title}” for processing via {resolved}.",
                level=messages.INFO,
            )

    def get_urls(self):
        # Custom URLs BEFORE super().get_urls() so they take precedence.
        custom = [
            path(
                "status-json/",
                self.admin_site.admin_view(self.status_json_view),
                name="documents_document_status_json",
            ),
            path(
                "<str:pk>/chunks-json/",
                self.admin_site.admin_view(self.chunks_json_view),
                name="documents_document_chunks_json",
            ),
            path(
                "<str:pk>/chunks/",
                self.admin_site.admin_view(self.chunks_view),
                name="documents_document_chunks",
            ),
        ]
        return custom + super().get_urls()

    def status_json_view(self, request):
        # Completion-notification data source: the changelist poller fetches this
        # every few seconds and shows a toast when a document flips to
        # ready/failed. GET only (no CSRF needed). Scoped to
        # self.get_queryset(request) so a user only ever sees their own rows;
        # limited to recently-updated documents to keep the payload small.
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        since = timezone.now() - timezone.timedelta(
            seconds=_STATUS_POLL_WINDOW_SECONDS
        )
        rows = (
            self.get_queryset(request)
            .filter(updated_at__gte=since)
            .values(
                "id", "title", "status", "chunk_count", "processing_error"
            )[:200]
        )
        return JsonResponse(
            {
                "documents": [
                    {
                        "id": str(row["id"]),
                        "title": row["title"],
                        "status": row["status"],
                        "chunk_count": row["chunk_count"],
                        "processing_error": row["processing_error"],
                    }
                    for row in rows
                ]
            }
        )

    def chunks_view(self, request, pk):
        # The HTML "Show" page (target of the show_chunks pill). Renders via
        # TemplateResponse with each_context so the Unfold shell/left sidebar
        # stay present (CRITICAL LAYOUT guardrail); the page fetches the scoped
        # JSON endpoint client-side and renders chunks with textContent/
        # createElement (never innerHTML). Resolve from the permission-scoped
        # queryset ONLY: out-of-scope documents 404.
        document = get_object_or_404(self.get_queryset(request), pk=pk)
        context = {
            **self.admin_site.each_context(request),
            "title": f"Chunks — {document.title}",
            "opts": self.model._meta,
            "original": document,
            "document": document,
            "change_url": reverse(
                "admin:documents_document_change", args=[document.pk]
            ),
            "chunks_json_url": reverse(
                "admin:documents_document_chunks_json", args=[document.pk]
            ),
        }
        return TemplateResponse(
            request, "admin/documents/document/chunks.html", context
        )

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

    def get_queryset(self, request):
        # Stash the request so embedding_preview (a list_display callable that
        # receives only ``obj``) can gate on request.user.is_superuser and read
        # the real vector back from Chroma.
        self._request = request
        return super().get_queryset(request)

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
        # Superuser-only: read the real vector back from Chroma (collection
        # 'pdf_chunks'), keyed by the chunk ULID, and render the screenshot's
        # truncated preview. Non-superusers (and any read failure / non-embedded
        # chunk) always see '—'. The vector lives in Chroma, not on the row.
        request = getattr(self, "_request", None)
        if request is None or not request.user.is_superuser:
            return "—"
        try:
            embeddings = ChromaVectorStore().get_embeddings(
                [str(obj.pk)], collection=VECTOR_COLLECTION
            )
        except Exception:  # noqa: BLE001 - preview is best-effort
            return "—"
        return _embedding_preview_text(embeddings.get(str(obj.pk)))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
