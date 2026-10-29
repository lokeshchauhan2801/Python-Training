"""Two-level reshape: slim Agent to a container, add the Step node.

Destructive wipe-and-reseed (blessed by the task): the flat Agent rows ARE the
old step-level data; rather than author a brittle chain->container+steps data
migration, drop every flat Agent row (its agents_agent_tools M2M cascades) and
let seed_agents rebuild the default flow. Tool rows are untouched.
"""

import basics.ulid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _wipe_agents(apps, schema_editor):
    """Hard-delete every flat Agent row (incl. soft-deleted) before the reshape.

    Clears agents_agent and its agents_agent_tools through table so dropping the
    old columns/M2M runs against an empty table. The historical base manager has
    no soft-delete filter, so this hits every row.
    """
    Agent = apps.get_model("agents", "Agent")
    Agent._base_manager.all().delete()


class Migration(migrations.Migration):

    # Non-atomic: the _wipe_agents RunPython DELETE must COMMIT before the
    # RemoveField ALTERs run, otherwise Postgres raises "cannot ALTER TABLE ...
    # because it has pending trigger events" (deferred FK checks from the
    # delete are still queued inside a single transaction).
    atomic = False

    dependencies = [
        ("company", "0001_initial"),
        ("agents", "0003_flat_tool_agent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Wipe flat Agent rows + their M2M before the schema reshape.
        migrations.RunPython(_wipe_agents, migrations.RunPython.noop),
        # 2. Slim Agent to a container (drop the six node fields; the
        #    agents_agent_slug_per_company constraint is unchanged).
        migrations.RemoveField(model_name="agent", name="tools"),
        migrations.RemoveField(model_name="agent", name="next_node"),
        migrations.RemoveField(model_name="agent", name="connected_from_start"),
        migrations.RemoveField(model_name="agent", name="connected_to_end"),
        migrations.RemoveField(model_name="agent", name="position_x"),
        migrations.RemoveField(model_name="agent", name="position_y"),
        # 3. Create the Step model + its tools M2M + partial-unique constraint.
        migrations.CreateModel(
            name="Step",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("is_deleted", models.BooleanField(db_index=True, default=False)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("id", basics.ulid.ULIDField(serialize=False)),
                ("slug", models.CharField(max_length=64)),
                ("name", models.CharField(blank=True, max_length=120)),
                (
                    "connected_from_start",
                    models.BooleanField(db_index=True, default=False),
                ),
                (
                    "connected_to_end",
                    models.BooleanField(db_index=True, default=False),
                ),
                ("position_x", models.FloatField(default=0)),
                ("position_y", models.FloatField(default=0)),
                ("order", models.IntegerField(db_index=True, default=0)),
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
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="agents.agent",
                    ),
                ),
                (
                    "next_step",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="prev_steps",
                        to="agents.step",
                    ),
                ),
            ],
            options={
                "db_table": "agents_step",
                "ordering": ["order", "-created_at"],
            },
        ),
        migrations.AddField(
            model_name="step",
            name="tools",
            field=models.ManyToManyField(
                blank=True, related_name="steps", to="agents.tool"
            ),
        ),
        migrations.AddConstraint(
            model_name="step",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_deleted", False)),
                fields=("agent", "slug"),
                name="agents_step_slug_per_agent",
            ),
        ),
    ]
