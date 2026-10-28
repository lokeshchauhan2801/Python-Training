from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class ConversationSession(BaseModel, SoftDeleteModel):
    session_id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    title = models.CharField(max_length=200, default="New session")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_sessions",
    )
    context = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    summary = models.TextField(blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "chat_conversation_session"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self) -> str:
        return self.title or str(self.session_id)


class Conversation(BaseModel, SoftDeleteModel):
    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    session = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_turns",
    )
    model_name = models.CharField(max_length=100, blank=True)
    question = models.TextField()
    response = models.TextField(blank=True)
    # ``context`` also carries render-state under reserved keys: ``context["chart"]``
    # (validated ChartData dict) and ``context["citations"]`` (citations list),
    # relocated here from the former ``chart_data``/``citations`` columns.
    context = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    documents = models.ManyToManyField(
        "documents.Document",
        related_name="conversation_turns",
        blank=True,
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "chat_conversation"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.session_id}: {self.question[:50]}"
    # NOTE: here ``self.session_id`` is the FK column of Conversation.session
    # (Django's auto ``<field>_id`` attribute). It now references
    # ConversationSession.session_id (the renamed ULID PK); the value is the
    # same ULID, so this keeps working unchanged.

    def _validate_company_matches_session(self):
        """A turn must belong to the same company as its session."""
        if self.company_id != self.session.company_id:
            raise ValidationError(
                {"company": "Conversation.company must match session.company."}
            )

    def clean(self):
        super().clean()
        self._validate_company_matches_session()

    def save(self, *args, **kwargs):


        self._validate_company_matches_session()
        super().save(*args, **kwargs)
