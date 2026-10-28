"""Flat two-table reshape: drop AgentGraph/AgentNode/AgentEdge, rework Tool, add Agent.

A clean drop of the old graph/node/edge data (and the old Tool rows, whose
``config["kind"]`` semantics do not map onto the new ``type``) is intentional —
no data-preservation logic.
"""

import basics.ulid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _wipe_tools(apps, schema_editor):
    """Clean slate: hard-delete every old Tool row (incl. soft-deleted).

    Reads NEITHER the ``config`` nor ``prompt_name`` column, so its position
    relative to the RemoveField ops is unconstrained.
    """
    Tool = apps.get_model("agents", "Tool")
    # Historical models expose only the base manager; this hits every row
    # (soft-deleted included) since the historical manager has no soft-delete
    # filter.
    Tool._base_manager.all().delete()


_TYPE_CHOICES = [
    ("general_assistant", "General assistant"),
    ("document_search", "Document search"),
    ("chat_summarise", "Chat summarise"),
    ("web_search", "Web search"),
    ("add_to_knowledge_base", "Add to knowledge base"),
    ("document_processing_status", "Document processing status"),
    ("langfuse", "Langfuse prompt"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("company", "0001_initial"),
        ("agents", "0002_tool_prompt_name"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Remove old constraints referencing tables/fields being dropped.
        migrations.RemoveConstraint(
            model_name="agentgraph",
            name="agents_one_active_graph_per_company",
        ),
        migrations.RemoveConstraint(
            model_name="agentedge",
            name="agents_edge_uniq",
        ),
        migrations.RemoveConstraint(
            model_name="agentnode",
            name="agents_node_slug_per_graph",
        ),
        # 2. Drop the M2M through table + graph/node/edge models (FK order:
        #    edge -> node -> graph).
        migrations.RemoveField(
            model_name="agentnode",
            name="tools",
        ),
        migrations.DeleteModel(name="AgentEdge"),
        migrations.DeleteModel(name="AgentNode"),
        migrations.DeleteModel(name="AgentGraph"),
        # 3. Rework Tool: wipe rows, drop old columns, add `type`.
        migrations.RunPython(_wipe_tools, migrations.RunPython.noop),
        migrations.RemoveField(model_name="tool", name="config"),
        migrations.RemoveField(model_name="tool", name="prompt_name"),
        migrations.AddField(
            model_name="tool",
            name="type",
            field=models.CharField(
                choices=_TYPE_CHOICES,
                default="general_assistant",
                max_length=32,
            ),
            preserve_default=False,
        ),
        # 4. Create the Agent model + its tools M2M + partial-unique constraint.
        migrations.CreateModel(
            name="Agent",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(db_index=True, default=False)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("id", basics.ulid.ULIDField(serialize=False)),
                ("slug", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=120)),
                ("connected_from_start", models.BooleanField(db_index=True, default=False)),
                ("connected_to_end", models.BooleanField(db_index=True, default=False)),
                ("position_x", models.FloatField(default=0)),
                ("position_y", models.FloatField(default=0)),
                ("order", models.IntegerField(db_index=True, default=0)),
                ("is_active", models.BooleanField(db_index=True, default=False)),
                (
                    "company",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="%(app_label)s_%(class)s_set",
                        to="company.company",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="%(app_label)s_%(class)s_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "next_node",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="prev_nodes",
                        to="agents.agent",
                    ),
                ),
            ],
            options={
                "db_table": "agents_agent",
                "ordering": ["order", "-created_at"],
            },
        ),
        migrations.AddField(
            model_name="agent",
            name="tools",
            field=models.ManyToManyField(
                blank=True, related_name="agents", to="agents.tool"
            ),
        ),
        migrations.AddConstraint(
            model_name="agent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False)),
                fields=("company", "slug"),
                name="agents_agent_slug_per_company",
            ),
        ),
    ]
