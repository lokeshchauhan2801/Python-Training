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
    """A named, independently-usable chat flow (container).

    An Agent groups an ordered set of Steps (its nodes) into a runnable
    START -> Step ... -> END pipeline. Multiple live Agents may coexist in a
    company; none is implicitly chosen for chat. A user selects one per
    ConversationSession (or Normal RAG) in the chat header.
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
    order = models.IntegerField(default=0, db_index=True)

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


class Step(BaseModel, SoftDeleteModel):
    """One node in an Agent's flow. Holds the tools; chained via next_step
    WITHIN its agent only. The pipeline runs steps entry -> next_step -> end."""

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        null=True,
        blank=True,
        db_index=True,
    )
    agent = models.ForeignKey(
        "agents.Agent",
        on_delete=models.CASCADE,
        related_name="steps",
    )
    slug = models.CharField(max_length=64)
    name = models.CharField(max_length=120, blank=True)
    tools = models.ManyToManyField("agents.Tool", blank=True, related_name="steps")
    connected_from_start = models.BooleanField(default=False, db_index=True)
    connected_to_end = models.BooleanField(default=False, db_index=True)
    next_step = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prev_steps",
    )
    position_x = models.FloatField(default=0)
    position_y = models.FloatField(default=0)
    order = models.IntegerField(default=0, db_index=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_step"
        ordering = ["order", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent", "slug"],
                condition=Q(is_deleted=False),
                name="agents_step_slug_per_agent",
            ),
        ]

    def __str__(self) -> str:
        return self.name or self.slug

    def clean(self):
        super().clean()
        # (1) step.company must match agent.company.
        if (
            self.agent_id is not None
            and self.company_id is not None
            and self.agent.company_id != self.company_id
        ):
            raise ValidationError(
                {"company": "step.company must match its agent's company"}
            )
        # (2) next_step (if set) must belong to the SAME agent and not be self.
        if self.next_step_id is not None:
            if self.next_step_id == self.pk:
                raise ValidationError(
                    {"next_step": "a step cannot point next_step at itself"}
                )
            if self.next_step.agent_id != self.agent_id:
                raise ValidationError(
                    {"next_step": "next_step must belong to the same agent"}
                )
        # NOTE: tools (M2M) are NOT validated here — M2M relations are not
        # populated until after save(). The "tools must belong to step.company"
        # invariant is enforced in the serializer (the project's style).
