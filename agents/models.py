from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class Tool(BaseModel, SoftDeleteModel):
    """A tool the registry can resolve (or a Langfuse-prompt-only tool),
    admin-togglable per company."""

    TYPE_GENERAL_ASSISTANT = "general_assistant"
    TYPE_DOCUMENT_SEARCH = "document_search"
    TYPE_CHAT_SUMMARISE = "chat_summarise"
    TYPE_WEB_SEARCH = "web_search"
    TYPE_ADD_TO_KB = "add_to_knowledge_base"
    TYPE_DOC_STATUS = "document_processing_status"
    TYPE_LANGFUSE = "langfuse"
    TYPE_CHOICES = [
        (TYPE_GENERAL_ASSISTANT, "General assistant"),
        (TYPE_DOCUMENT_SEARCH, "Document search"),
        (TYPE_CHAT_SUMMARISE, "Chat summarise"),
        (TYPE_WEB_SEARCH, "Web search"),
        (TYPE_ADD_TO_KB, "Add to knowledge base"),
        (TYPE_DOC_STATUS, "Document processing status"),
        (TYPE_LANGFUSE, "Langfuse prompt"),
    ]

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        null=True,
        blank=True,
        db_index=True,
    )
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    description = models.TextField(blank=True)
    is_enabled = models.BooleanField(default=True, db_index=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_tool"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                condition=Q(is_deleted=False),
                name="agents_tool_slug_per_company",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    @property
    def prompt_name(self) -> str:
        """Compat shim for llm_tools handlers (which read ``tool.prompt_name``).

        For a Langfuse-type tool the ``slug`` IS the Langfuse prompt pointer, so
        return it. For every other type return "" so ``_system_prompt`` falls
        back to the handler's built-in ``default_name`` (e.g.
        ``tool_general_assistant``). This keeps ``llm_tools.py`` untouched after
        the ``prompt_name`` column is dropped.
        """
        return self.slug if self.type == self.TYPE_LANGFUSE else ""

    def clean(self):
        super().clean()
        # Lazy import avoids a module-scope chat<-agents cycle and keeps the
        # registry the source of truth for handler-backed tools.
        from chat.services.langgraph_service import available_slugs

        # type == "langfuse" is prompt-only: no registered handler is needed
        # (the slug is a Langfuse prompt pointer). Every other type's slug MUST
        # resolve in the registry.
        if self.type != self.TYPE_LANGFUSE and self.slug not in available_slugs():
            raise ValidationError(
                {"slug": f"Unknown tool slug '{self.slug}' (no handler registered)."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class Agent(BaseModel, SoftDeleteModel):
    """One step node in a company's chat flow.

    The flow is a linear chain: the entry Agent has connected_from_start=True;
    each Agent points at the next via next_node; the final Agent has
    connected_to_end=True. The active flow for a company is the chain whose
    entry Agent has is_active=True.
    """

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        null=True,
        blank=True,
        db_index=True,
    )
    slug = models.CharField(max_length=64)
    name = models.CharField(max_length=120, blank=True)
    tools = models.ManyToManyField(Tool, blank=True, related_name="agents")
    connected_from_start = models.BooleanField(default=False, db_index=True)
    connected_to_end = models.BooleanField(default=False, db_index=True)
    next_node = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prev_nodes",
    )
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=False, db_index=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_agent"
        ordering = ["order", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                condition=Q(is_deleted=False),
                name="agents_agent_slug_per_company",
            ),
        ]

    def __str__(self) -> str:
        return self.name or self.slug
