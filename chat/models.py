"""Chat domain models.

``ConversationSession`` groups a sequence of question/response ``Conversation``
turns. Both inherit ``BaseModel`` (UUID pk, required company FK, audit +
soft-delete). The cross-field invariant ``Conversation.company ==
session.company`` is owned by the model: it is declared in ``clean()`` AND
re-checked inside ``save()`` so admin, serializer, and service saves are all
guarded (a cross-table rule cannot be a DB ``CheckConstraint``).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel
from basics.ulid import new_ulid


class ConversationSession(BaseModel):
    session_id = models.CharField(
        max_length=26, unique=True, default=new_ulid, editable=False
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
            models.Index(fields=["session_id"]),
        ]

    def __str__(self) -> str:
        return self.title or self.session_id


class Conversation(BaseModel):
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
        # Universal backstop: re-check the cross-field invariant on every save
        # path (admin direct-save, serializer, service) before persisting.
        self._validate_company_matches_session()
        super().save(*args, **kwargs)
