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

A library ``Document`` added in the admin is NOT processed on save: it lands in
the ``uploaded`` state and waits. The user then chooses a backend from the in-row
``Schedule`` dropdown (Process now / Celery / Kafka / Temporal) to start it.
"""

from django.contrib import admin, messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from unfold.decorators import action
from unfold.enums import ActionVariant

from basics.admin import BaseModelAdmin
from basics.vectorstore import ChromaVectorStore

from infrastructure.processing.temporal_backend import TemporalProcessingBackend

from .forms import DocumentAdminForm
from .models import Document, DocumentChunk, DocumentStage
from .services import VECTOR_COLLECTION, purge_document
from .tasks import enqueue_document_processing

# The four options the single "Schedule" dropdown/chooser offers, in display
# order. Each maps to how the existing dispatcher is invoked:
#   * "now"      → dispatch via the document's current/default backend (the old
#                  "Process now": DEBUG celery inline / synchronous path).
#   * "celery"   → set processing_backend='celery' and dispatch via celery.
#   * "kafka"    → set processing_backend='kafka' and dispatch via kafka.
#   * "temporal" → set processing_backend='temporal' and dispatch via temporal.
_SCHEDULE_CHOICES = (
    ("now", "Process now"),
    ("celery", "Celery"),
    ("kafka", "Kafka"),
    ("temporal", "Temporal"),
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

    # --- Real (hard) delete -------------------------------------------------
    # The base admin's delete does a SOFT delete (flips is_deleted), which left
    # the row visible in the changelist (which shows all_objects) and never
    # removed the uploaded file or the Chroma vectors — so "Successfully deleted"
    # appeared while nothing actually went away. For Documents, delete means
    # delete: purge_document removes the vectors, the chunk rows, the file on
    # disk, and the Document row itself. The explicit "Soft delete selected" /
    # "Restore selected" actions (from BaseModelAdmin) remain available for the
    # recoverable soft-delete workflow.
    def delete_model(self, request, obj):
        purge_document(obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            purge_document(obj)

    # Changelist override adds the completion-notification status poller while
    # keeping the house-style/Unfold changelist intact.
    change_list_template = "admin/documents/document/change_list.html"

    # Per-row actions (ActionVariant.PRIMARY). All resolve the row from the
    # scoped queryset. "Show" opens the scoped chunk view. The processing
    # backend chooser is NOT a row-action anymore: it is the in-row "Schedule"
    # column below (an "Action" button + inline dropdown that POSTs to the
    # AJAX dispatch endpoint — no separate page, no full reload).
    actions_row = ("show_chunks", "info_action")

    list_display = (
        "id_short",
        "title_with_progress",
        "file_type",
        "status_badge",
        "chunk_count",
        "processing_backend",
        "schedule_column",
        "created_by",
        "created_at",
    )
    list_filter = ("status", "file_type", "source", "company", "is_deleted", "created_at")
    search_fields = ("title", "uploaded_by__email")
    date_hierarchy = "created_at"

    # The ADD form is intentionally minimal: the user only chooses a file OR a
    # media URL. Everything else (title, source, file_type, status, processing
    # fields, tenant/audit) is set automatically server-side (see save_model and
    # the process-time derivation in documents.services.process_document), so it
    # is hidden from the add form entirely.
    add_fieldsets = (
        (
            None,
            {
                "fields": ("file", "source_url"),
                "description": (
                    "Upload a file OR paste a media URL (exactly one). A URL is "
                    "downloaded in the background by the scrapper. The title and "
                    "file type are filled in automatically after processing."
                ),
            },
        ),
    )

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

    def get_fieldsets(self, request, obj=None):
        # ADD (obj is None): the minimal file-OR-URL fieldset only — nothing
        # from the richer change layout (title/source/file_type/processing/
        # tenant/audit) is exposed. CHANGE: the full read-only-aware layout.
        if obj is None:
            return self.add_fieldsets
        return super().get_fieldsets(request, obj)

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

    @admin.display(description="Title")
    def title_with_progress(self, obj):
        progress = obj.processing_progress_display
        stage = obj.processing_stage or obj.status
        remaining = 100 - progress
        return format_html(
            '<div class="document-progress" data-document-id="{}">'
            '<div class="document-progress-title">{}</div>'
            '<div class="document-progress-details" data-progress-details>'
            '<span data-progress-percent>{}% processed</span>'
            '<span data-progress-remaining>{}% left</span>'
            '<span data-progress-stage>{}</span>'
            '</div>'
            '<div class="document-progress-track" role="progressbar" '
            'aria-label="Document processing progress" aria-valuemin="0" '
            'aria-valuemax="100" aria-valuenow="{}">'
            '<span data-progress-bar style="width:{}%"></span>'
            '</div></div>',
            str(obj.pk),
            obj.title,
            progress,
            remaining,
            stage.replace("_", " ").title(),
            progress,
            progress,
        )

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        color = _STATUS_COLORS.get(obj.status, "#6b7280")
        return format_html(
            '<span style="color:{}; font-weight:600;">{}</span>',
            color,
            obj.get_status_display(),
        )

    # --- Schedule column (in-row inline dropdown) ---------------------------
    @admin.display(description="Schedule")
    def schedule_column(self, obj):
        # Renders the in-row "Schedule" control: an "Action" button plus a
        # hidden dropdown menu (the four processing options). The control is
        # emitted for EVERY library row regardless of status (uploaded, queued,
        # processing, ready, failed) so a Ready document can be re-dispatched
        # (reprocess). Clicking "Action" toggles the dropdown open just below
        # the button, pinned with position:fixed by schedule_dropdown.js so it
        # is never clipped by the changelist's overflow container; selecting an
        # item POSTs to the AJAX dispatch endpoint without navigating away.
        # Only library documents can be processed, so non-library rows (e.g. a
        # chat attachment) show a disabled hint ('—') instead of the control.
        #
        # The markup is a fixed template with ONLY server-controlled values
        # (pk, backend keys, static labels) interpolated via format_html's auto-
        # escaping — never raw user text — so nothing here can inject markup.
        if obj.source != Document.SOURCE_LIBRARY:
            return format_html(
                '<span class="text-font-subtle-light '
                'dark:text-font-subtle-dark text-sm">—</span>'
            )

        items = format_html_join(
            "",
            (
                '<button type="button" role="menuitem" '
                'class="schedule-menu-item block w-full text-left px-4 py-2 '
                'text-sm text-font-default-light dark:text-font-default-dark '
                'hover:bg-primary-50 dark:hover:bg-primary-900/40 '
                'hover:text-primary-600 dark:hover:text-primary-400 '
                'cursor-pointer" '
                'data-doc-id="{}" data-backend="{}">{}</button>'
            ),
            (
                (str(obj.pk), value, label)
                for value, label in _SCHEDULE_CHOICES
            ),
        )
        # The menu is NOT absolutely positioned inside the cell anymore: the
        # changelist table lives in an overflow container that clipped an
        # absolute/z-50 menu ("goes under the list line"). Positioning is now
        # owned by schedule_dropdown.js, which on open pins the menu with
        # position:fixed at the toggle button's viewport rect and a high
        # z-index so it renders in a non-clipped layer. The markup therefore
        # keeps only the look (width/border/shadow) and the 'hidden' toggle
        # class; it carries NO 'absolute'/'z-50' / 'mt-1' positioning utilities.
        return format_html(
            '<div class="schedule-cell inline-block text-left">'
            '  <button type="button" '
            '          class="schedule-toggle inline-flex items-center gap-1 '
            'rounded-default border border-primary-600 bg-primary-600 '
            'px-3 py-1 text-sm font-medium text-white '
            'hover:bg-primary-700 cursor-pointer" '
            '          aria-haspopup="true" aria-expanded="false" '
            '          data-doc-id="{}">'
            '    <span>Action</span>'
            '    <span class="material-symbols-outlined text-base '
            'leading-none">expand_more</span>'
            '  </button>'
            '  <div class="schedule-menu hidden w-44 '
            'origin-top-left rounded-default border border-base-200 '
            'dark:border-base-700 bg-white dark:bg-base-800 shadow-lg py-1" '
            '       role="menu" aria-orientation="vertical">{}</div>'
            '</div>',
            str(obj.pk),
            items,
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

    @action(
        description="Info",
        url_path="temporal-info-pill",
        variant=ActionVariant.PRIMARY,
    )
    def info_action(self, request, object_id):
        # The per-row "Info" pill. Unfold's actions_row expects every entry to
        # be an @action, but this one never navigates: temporal_info.js
        # intercepts the click (preventDefault) and opens the polling modal
        # instead. The returned button carries data-doc-id and the 'info-action'
        # class the JS delegates on. object_id is a server-controlled pk
        # interpolated via format_html's auto-escaping — no raw user text.
        return format_html(
            '<button type="button" class="info-action '
            'inline-flex items-center gap-1 rounded-default border '
            'border-primary-600 bg-primary-600 px-3 py-1 text-sm '
            'font-medium text-white hover:bg-primary-700 cursor-pointer" '
            'data-doc-id="{}">Info</button>',
            str(object_id),
        )

    def save_model(self, request, obj, form, change):
        # The minimal ADD form omits ``source``; a document added through that
        # form is always a library upload, so stamp it BEFORE super().save_model
        # (which enforces company + stamps uploaded_by). The title/file_type are
        # left to process-time derivation. Only force this when the submitted
        # form actually omitted ``source`` (the add form) — a programmatic
        # save_model (form=None) or any form that does expose ``source`` keeps
        # the caller's chosen source (e.g. a chat attachment).
        if not change and form is not None and "source" not in form.fields:
            obj.source = Document.SOURCE_LIBRARY
        # BaseModelAdmin stamps the actor (uploaded_by=request.user) + enforces
        # the owned company server-side.
        super().save_model(request, obj, form, change)
        # A library document is NOT auto-dispatched on save. It lands in the
        # ``uploaded`` state and waits; the user then chooses WHERE to process it
        # from the in-row **Schedule** dropdown (Process now / Celery / Kafka /
        # Temporal). This gives the user explicit control over the backend
        # instead of silently kicking off extraction the moment they click Save.
        # Chat attachments were never auto-processed either (Phase 3 wires that
        # flow), so no dispatch happens here for any source.
        if obj.source == Document.SOURCE_LIBRARY and not change:
            self.message_user(
                request,
                f"“{obj.title}” was added. Choose a backend from the "
                f"Schedule column to start processing.",
                level=messages.INFO,
            )

    def get_urls(self):
        # Custom URLs BEFORE super().get_urls() so they take precedence.
        custom = [
            path(
                "<str:pk>/temporal-info/",
                self.admin_site.admin_view(self.temporal_info_view),
                name="documents_document_temporal_info",
            ),
            path(
                "queue/",
                self.admin_site.admin_view(self.queue_view),
                name="documents_document_queue",
            ),
            path(
                "queue-json/",
                self.admin_site.admin_view(self.queue_json_view),
                name="documents_document_queue_json",
            ),
            path(
                "status-json/",
                self.admin_site.admin_view(self.status_json_view),
                name="documents_document_status_json",
            ),
            path(
                "<str:pk>/dispatch/",
                self.admin_site.admin_view(self.dispatch_view),
                name="documents_document_dispatch",
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

    def queue_view(self, request):
        if not self.has_view_or_change_permission(request):
            return HttpResponseForbidden()
        documents = self.get_queryset(request).filter(
            source=Document.SOURCE_LIBRARY,
            status__in=(
                Document.STATUS_QUEUED,
                Document.STATUS_PROCESSING,
                Document.STATUS_FAILED,
            ),
        )[:500]
        context = {
            **self.admin_site.each_context(request),
            "title": "Queue messages",
            "opts": self.model._meta,
            "documents": documents,
            "queue_json_url": reverse(
                "admin:documents_document_queue_json"
            ),
        }
        return TemplateResponse(
            request, "admin/documents/document/queue.html", context
        )

    def queue_json_view(self, request):
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        if not self.has_view_or_change_permission(request):
            return HttpResponseForbidden()
        rows = (
            self.get_queryset(request)
            .filter(
                source=Document.SOURCE_LIBRARY,
                status__in=(
                    Document.STATUS_QUEUED,
                    Document.STATUS_PROCESSING,
                    Document.STATUS_FAILED,
                ),
            )
            .values(
                "id",
                "title",
                "status",
                "processing_stage",
                "processing_progress",
                "processing_backend",
                "processing_task_id",
                "processing_error",
                "updated_at",
            )
            .order_by("-updated_at")[:500]
        )
        status_display = dict(Document.STATUS_CHOICES)
        return JsonResponse(
            {
                "documents": [
                    {
                        **row,
                        "id": str(row["id"]),
                        "status_display": status_display[row["status"]],
                        "processing_progress": (
                            100
                            if row["status"] == Document.STATUS_READY
                            else row["processing_progress"]
                        ),
                        "updated_at": row["updated_at"].isoformat(),
                    }
                    for row in rows
                ]
            }
        )

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
                "id",
                "title",
                "status",
                "chunk_count",
                "processing_error",
                "processing_progress",
                "processing_stage",
            )[:200]
        )
        return JsonResponse(
            {
                "documents": [
                    {
                        "id": str(row["id"]),
                        "title": row["title"],
                        "status": row["status"],
                        "status_display": dict(Document.STATUS_CHOICES)[
                            row["status"]
                        ],
                        "chunk_count": row["chunk_count"],
                        "processing_error": row["processing_error"],
                        "processing_progress": (
                            100
                            if row["status"] == Document.STATUS_READY
                            else row["processing_progress"]
                        ),
                        "processing_stage": row["processing_stage"],
                    }
                    for row in rows
                ]
            }
        )

    def temporal_info_view(self, request, pk):
        # Data source for the per-row "Info" modal (temporal_info.js). GET only
        # (405 otherwise); CSRF is irrelevant for a read. Resolve from the
        # permission-scoped queryset ONLY (out-of-scope → 404). The Stages table
        # is built from the document's DocumentStage rows ordered by `order`,
        # with a single synthetic fallback row derived from the document's own
        # processing_stage/status when no stage rows exist yet (e.g. a document
        # that has never been dispatched, or processed by a non-Temporal
        # backend). The live Temporal workflow describe() is only consulted when
        # processing_backend == "temporal"; for any other backend we return
        # status "n/a" + a note and never touch Temporal.
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        document = get_object_or_404(self.get_queryset(request), pk=pk)

        stages = list(
            DocumentStage.objects.filter(document=document)
            .order_by("order")
            .values("name", "status", "detail")
        )
        if not stages:
            # Synthetic single-entry fallback: surface the document's own
            # coarse progress so the modal always shows something meaningful.
            stage_name = document.processing_stage or document.status
            stages = [
                {
                    "name": stage_name or "processing",
                    "status": document.status,
                    "detail": document.processing_error or "",
                }
            ]

        workflow_id = f"doc-{document.pk}"
        if document.processing_backend == "temporal":
            info = TemporalProcessingBackend().get_status(workflow_id)
            payload = {
                "workflow_id": workflow_id,
                "status": info.get("status", "unknown"),
                "run_id": info.get("run_id"),
                "started": info.get("started"),
                "closed": info.get("closed"),
                "result": info.get("result"),
                "degraded": info.get("degraded", False),
                "note": "",
                "stages": stages,
            }
        else:
            # Non-Temporal backend: never touch Temporal.
            payload = {
                "workflow_id": workflow_id,
                "status": "n/a",
                "run_id": None,
                "started": None,
                "closed": None,
                "result": None,
                "degraded": False,
                "note": (
                    "This document is processed by the "
                    f"'{document.processing_backend}' backend, not Temporal."
                ),
                "stages": stages,
            }
        return JsonResponse(payload)

    def dispatch_view(self, request, pk):
        # AJAX dispatch endpoint for the in-row "Schedule" dropdown. POST only
        # (GET → 405) and CSRF-protected by the admin_view wrapper. Resolves the
        # document from the permission-scoped queryset ONLY (out-of-scope → 404)
        # and dispatches via the EXISTING seam:
        #   * "now"      → dispatch via the document's current/default backend
        #                  (DEBUG celery inline / synchronous path), exactly as
        #                  the old "Process now".
        #   * celery/kafka/temporal → record processing_backend and dispatch via
        #                  that backend.
        # Returns JSON the dropdown JS uses to update the row's status cell;
        # never navigates and never does a full-page reload.
        if request.method != "POST":
            return JsonResponse({"detail": "Method not allowed."}, status=405)
        document = get_object_or_404(self.get_queryset(request), pk=pk)
        if document.source != Document.SOURCE_LIBRARY:
            return JsonResponse(
                {"detail": "Only library documents can be processed."},
                status=400,
            )
        choice = request.POST.get("backend", "")
        valid = {value for value, _label in _SCHEDULE_CHOICES}
        if choice not in valid:
            return JsonResponse(
                {"detail": "Pick a valid option (now, celery, kafka, temporal)."},
                status=400,
            )
        # "Process now" dispatches via the document's current/default backend
        # (no explicit backend); the other three set the backend explicitly.
        backend = None if choice == "now" else choice
        if backend is not None:
            document.processing_backend = backend
        self._reset_for_reprocess(document)
        _job_id, resolved = enqueue_document_processing(
            str(document.pk), backend=backend
        )
        return JsonResponse(
            {
                "status": Document.STATUS_QUEUED,
                "status_display": dict(Document.STATUS_CHOICES)[
                    Document.STATUS_QUEUED
                ],
                "backend": resolved,
                "document_id": str(document.pk),
                "title": document.title,
                "message": (
                    f"Task queued for processing via {resolved}."
                ),
            },
            status=202,
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
