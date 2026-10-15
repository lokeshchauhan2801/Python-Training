"""Chat domain models.

``ConversationSession`` groups a sequence of question/response ``Conversation``
turns. Both compose ``BaseModel`` (audit) + ``SoftDeleteModel`` + a ULID ``id``
PK and declare their own required ``company`` FK directly. The cross-field invariant
``Conversation.company ==
session.company`` is owned by the model: it is declared in ``clean()`` AND
re-checked inside ``save()`` so admin, serializer, and service saves are all
guarded (a cross-table rule cannot be a DB ``CheckConstraint``).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField, new_ulid


class ConversationSession(BaseModel, SoftDeleteModel):
    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
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


class Message(BaseModel, SoftDeleteModel):
    """A single chat message (user/assistant/system) inside a session.

    The RAG chat UI (Phase 3) persists one ``Message`` per turn side, carrying
    the answer's citations, an optional structured ``chart_data`` payload, the
    documents it is grounded in, and the ``context_signature`` the repeated-query
    cache keys on. Like every chat row it composes ``BaseModel`` + ``SoftDeleteModel``
    with a ULID PK and a required ``company`` FK, and enforces
    ``company == conversation.company`` in both ``clean()`` and ``save()``.
    """

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_SYSTEM = "system"
    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_ASSISTANT, "Assistant"),
        (ROLE_SYSTEM, "System"),
    ]

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    conversation = models.ForeignKey(
        ConversationSession,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="children",
        null=True,
        blank=True,
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_USER)
    content = models.TextField()
    citations = models.JSONField(default=list, blank=True)
    chart_data = models.JSONField(null=True, blank=True)
    documents = models.ManyToManyField(
        "documents.Document",
        related_name="chat_messages",
        blank=True,
    )
    model_name = models.CharField(max_length=100, default="gpt-4o-mini")
    # Document-context signature in effect when this answer was produced. Used
    # by the repeated-query cache to detect whether the retrievable document
    # context changed between two identical questions. NULL on legacy rows is
    # treated as unknown => cache miss. See ChatService._context_signature.
    context_signature = models.CharField(max_length=64, null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "chat_message"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.role}: {self.content[:40]}"

    def _validate_company_matches_conversation(self):
        """A message must belong to the same company as its conversation."""
        if self.company_id != self.conversation.company_id:
            raise ValidationError(
                {"company": "Message.company must match conversation.company."}
            )

    def clean(self):
        super().clean()
        self._validate_company_matches_conversation()

    def save(self, *args, **kwargs):
        # Universal backstop: re-check the cross-field invariant on every save
        # path (admin direct-save, serializer, service) before persisting.
        self._validate_company_matches_conversation()
        super().save(*args, **kwargs)


class MessageVersion(BaseModel, SoftDeleteModel):
    """A historical content revision of a ``Message`` (edit/regenerate trail)."""

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    content = models.TextField()
    version = models.PositiveIntegerField()

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "chat_message_version"
        constraints = [
            models.UniqueConstraint(
                fields=["message", "version"],
                name="chat_message_version_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.message_id} v{self.version}"
