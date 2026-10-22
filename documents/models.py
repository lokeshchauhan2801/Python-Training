from django.conf import settings
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class Document(BaseModel, SoftDeleteModel):
    STATUS_UPLOADED = "uploaded"
    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_READY = "ready"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_READY, "Ready"),
        (STATUS_FAILED, "Failed"),
    ]
    SOURCE_CHAT = "chat"
    SOURCE_LIBRARY = "library"
    SOURCE_CHOICES = [
        (SOURCE_CHAT, "Chat attachment"),
        (SOURCE_LIBRARY, "Document library"),
    ]

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    session = models.ForeignKey(
        "chat.ConversationSession",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_CHAT,
        db_index=True,
    )
    title = models.CharField(max_length=255)
    file = models.FileField(upload_to="documents/%Y/%m/%d/", blank=True)
    source_url = models.URLField(blank=True, default="")
    file_type = models.CharField(max_length=20, default="pdf")
    content_type = models.CharField(max_length=150, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_UPLOADED
    )
    processing_backend = models.CharField(max_length=50, default="celery")
    processing_stage = models.CharField(max_length=40, blank=True)
    processing_progress = models.PositiveSmallIntegerField(default=0)
    processing_task_id = models.CharField(max_length=255, blank=True)
    processing_error = models.TextField(blank=True)
    chunk_count = models.PositiveIntegerField(default=0)
    processing_attempts = models.PositiveSmallIntegerField(default=0)

    processing_duration_ms = models.PositiveIntegerField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "documents_document"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["uploaded_by", "status"])]

    def __str__(self) -> str:
        return self.title

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        super().clean()
        if self.source != self.SOURCE_LIBRARY:
            return
        has_file = bool(self.file)
        has_url = bool(self.source_url)
        if has_file and has_url:
            raise ValidationError(
                "Provide EITHER a file OR a media URL, not both."
            )
        if not has_file and not has_url:
            raise ValidationError(
                "A library document needs a file upload or a media URL."
            )

    @property
    def processing_duration_display(self) -> str:
        """Human-readable "time taken to convert" (e.g. "1.8 s", "420 ms")."""
        ms = self.processing_duration_ms
        if ms is None:
            return "—"
        if ms < 1000:
            return f"{ms} ms"
        return f"{ms / 1000:.1f} s"

    @property
    def processing_progress_display(self) -> int:
        """Return a progress value consistent with the document's final state."""
        if self.status == self.STATUS_READY:
            return 100
        return min(100, max(0, int(self.processing_progress or 0)))

    @property
    def processing_remaining_display(self) -> int:
        """Return the percentage remaining for the displayed progress value."""
        return 100 - self.processing_progress_display


class DocumentChunk(BaseModel, SoftDeleteModel):
    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    chunk_index = models.PositiveIntegerField()
    page_number = models.PositiveIntegerField()
    source_location = models.CharField(max_length=255, blank=True)
    content = models.TextField()

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "documents_document_chunk"
        ordering = ["document", "chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "chunk_index"],
                name="documents_chunk_index_uniq",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.document.title} · page {self.page_number} "
            f"· chunk {self.chunk_index}"
        )


class DocumentStage(BaseModel, SoftDeleteModel):
    """Per-document processing stage row for the resumable Temporal pipeline.

    One row per canonical stage (see ``documents.services.DOCUMENT_STAGES``); a
    stage completed on a prior run short-circuits on resume, so a workflow that
    failed mid-pipeline restarts at the first non-completed stage.
    """

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    name = models.CharField(max_length=40)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    detail = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "documents_document_stage"
        ordering = ["document", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "name"],
                name="documents_stage_name_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} · {self.name} · {self.status}"
