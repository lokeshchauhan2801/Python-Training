"""Models for the admin-configurable LangGraph agent builder.

Tool / AgentGraph / AgentNode / AgentEdge. Like every model: ULID PK,
BaseModel + SoftDeleteModel, explicit db_table. One deviation: company is
NULLABLE (to allow a future global/template graph); the runtime always scopes
by a concrete company. Single-table rules are partial unique constraints;
cross-table invariants live in clean() and are re-checked in save().
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from basics.managers import AllObjectsManager, SoftDeleteManager
from basics.models import BaseModel, SoftDeleteModel
from basics.ulid import ULIDField


class AgentGraph(BaseModel, SoftDeleteModel):
    """One configurable graph per company — the unit the chat path looks up."""

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        null=True,
        blank=True,
        db_index=True,
    )
    name = models.CharField(max_length=200, default="Default agent")
    is_active = models.BooleanField(default=False, db_index=True)
    description = models.TextField(blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_agent_graph"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["company"],
                condition=Q(is_active=True, is_deleted=False),
                name="agents_one_active_graph_per_company",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Tool(BaseModel, SoftDeleteModel):
    """A tool the registry can resolve, admin-togglable per company."""

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
    config = models.JSONField(default=dict, blank=True)
    # Langfuse prompt pointer (the managed prompt's NAME, not its text). Blank
    # => the handler uses its hard-coded fallback prompt.
    prompt_name = models.CharField(max_length=200, blank=True, default="")

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

    def clean(self):
        super().clean()
        # The slug must resolve to a handler in the in-code tool registry. This
        # is a cross-reference to CODE (not another table), so it lives in model
        # validation. Imported lazily so importing agents.models never imports
        # the registry at module load.
        from agents.tools import available_slugs

        if self.slug not in available_slugs():
            raise ValidationError(
                {"slug": f"Unknown tool slug '{self.slug}' (no handler registered)."}
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class AgentNode(BaseModel, SoftDeleteModel):
    """A node in a graph. v1 ships START/STEP/END; the model supports many
    STEP nodes for the canvas later."""

    KIND_START = "start"
    KIND_STEP = "step"
    KIND_END = "end"
    KIND_CHOICES = [
        (KIND_START, "Start"),
        (KIND_STEP, "Step"),
        (KIND_END, "End"),
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
    graph = models.ForeignKey(
        AgentGraph,
        on_delete=models.CASCADE,
        related_name="nodes",
    )
    slug = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    label = models.CharField(max_length=120, blank=True)
    tools = models.ManyToManyField(Tool, blank=True, related_name="nodes")
    prompt_name = models.CharField(max_length=120, blank=True)
    position = models.JSONField(default=dict, blank=True)
    config = models.JSONField(default=dict, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_agent_node"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["graph", "slug"],
                condition=Q(is_deleted=False),
                name="agents_node_slug_per_graph",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.slug} [{self.kind}]"

    def _validate_company_matches_graph(self):
        if self.company_id != self.graph.company_id:
            raise ValidationError(
                {"company": "AgentNode.company must match graph.company."}
            )

    def clean(self):
        super().clean()
        self._validate_company_matches_graph()
        # At most one start and at most one end per graph. This needs a sibling
        # query (the save-time backstop would race), so it is a best-effort
        # clean()-only validation; the runtime treats "no START" as "no graph
        # configured" -> fallback.
        if self.kind in (self.KIND_START, self.KIND_END):
            siblings = AgentNode.objects.filter(
                graph=self.graph, kind=self.kind
            ).exclude(pk=self.pk)
            if siblings.exists():
                raise ValidationError(
                    {"kind": f"A graph may have only one '{self.kind}' node."}
                )

    def save(self, *args, **kwargs):
        self._validate_company_matches_graph()
        super().save(*args, **kwargs)


class AgentEdge(BaseModel, SoftDeleteModel):
    """A directed edge for the canvas / deterministic traversal."""

    id = ULIDField()
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        null=True,
        blank=True,
        db_index=True,
    )
    graph = models.ForeignKey(
        AgentGraph,
        on_delete=models.CASCADE,
        related_name="edges",
    )
    source = models.ForeignKey(
        AgentNode,
        on_delete=models.CASCADE,
        related_name="out_edges",
    )
    target = models.ForeignKey(
        AgentNode,
        on_delete=models.CASCADE,
        related_name="in_edges",
    )
    condition = models.CharField(max_length=120, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        db_table = "agents_agent_edge"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "target"],
                condition=Q(is_deleted=False),
                name="agents_edge_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_id} -> {self.target_id}"

    def _validate_edge(self):
        # No self-loop.
        if self.source_id == self.target_id:
            raise ValidationError(
                {"target": "An edge cannot connect a node to itself."}
            )
        # No cross-graph edge.
        if self.source.graph_id != self.target.graph_id:
            raise ValidationError(
                {"target": "An edge must connect nodes of the same graph."}
            )
        # company == source.graph.company == target.graph.company.
        if (
            self.company_id != self.source.graph.company_id
            or self.company_id != self.target.graph.company_id
        ):
            raise ValidationError(
                {"company": "AgentEdge.company must match the graph's company."}
            )

    def clean(self):
        super().clean()
        self._validate_edge()

    def save(self, *args, **kwargs):
        self._validate_edge()
        super().save(*args, **kwargs)
