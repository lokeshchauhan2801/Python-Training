from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class ConversationSession(BaseModel, SoftDeleteModel):
    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    title = models.CharField(max_length=200, default="New session")
    content = models.TextField(blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_sessions",
    )
    model_name = models.CharField(max_length=100, blank=True)
    context = models.JSONField(default=dict, blank=True)
    media = models.JSONField(default=list, blank=True)
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
        return self.title or str(self.id)


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
    context = models.JSONField(default=dict, blank=True)
    media = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    citations = models.JSONField(default=list, blank=True)
    chart_data = models.JSONField(null=True, blank=True)
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
    # NOTE: here ``session_id`` is the FK id of Conversation.session (auto
    # attribute), NOT the removed ConversationSession.session_id field.

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
