from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class Tool(BaseModel, SoftDeleteModel):
    """A tool attached to agent steps, admin-togglable per company.

    A slug with a registered handler runs that built-in tool; any other slug
    is a prompt-only tool. Either way ``description`` is the tool's docs: its
    role and the instructions it runs with.
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
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=120)
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
    def is_prompt_only(self) -> bool:
        """True when no built-in handler has this slug (it runs on its docs)."""
        # Lazy import avoids a module-scope chat<-agents cycle.
        from chat.agent.tools import available_slugs

        return self.slug not in available_slugs()


class Agent(BaseModel, SoftDeleteModel):
    """A named, independently-usable chat flow (container).

    The builder edits an Agent's flow as Steps (START -> Step ... -> END).
    Every Step change rewrites ``graph``, the flow as LangGraph-style graph
    JSON (``agents/services/graph_json.py``); chat runs that JSON only and
    never reads Steps. Multiple live Agents may coexist in a
    company; none is implicitly chosen for chat. An Agent without a company is
    global: every company can use it. A user selects one per
    ConversationSession (or Normal RAG) in the chat header.

    The agent's role (what it is for, which tasks it can do) is its Langfuse
    prompt, not a column: see ``agents/services/agent_prompts.py``.
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
    graph = models.JSONField(default=dict, blank=True)
    # Canvas-only: where the builder draws START and END,
    # ``{"start": {"x": .., "y": ..}, "end": {"x": .., "y": ..}}``.
    layout = models.JSONField(default=dict, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_agent"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                condition=Q(is_deleted=False),
                name="agents_agent_slug_per_company",
            ),
        ]

    def __str__(self) -> str:
        return self.name or self.slug

    def clean(self):
        super().clean()
        # The slug is the agent's Langfuse prompt name: a "/" would make a
        # folder there.
        if "/" in (self.slug or ""):
            raise ValidationError({"slug": "an agent slug cannot contain '/'"})


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
