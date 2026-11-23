"""Give every Agent its own chat route.

``Agent.route`` is the URL segment of the agent's chat endpoint
(``/chat/<session>/send/<route>/``). Existing agents get their slug as route.
"""

from django.db import migrations, models
from django.db.models import Q


def fill_routes(apps, schema_editor):
    import re

    Agent = apps.get_model("agents", "Agent")
    for agent in Agent._base_manager.filter(route=""):
        route = re.sub(r"[^a-z0-9_-]+", "-", (agent.slug or "").lower()).strip("-_")[:64]
        Agent._base_manager.filter(pk=agent.pk).update(route=route)


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0019_tool_docs_process_destination"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="route",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.RunPython(fill_routes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="agent",
            constraint=models.UniqueConstraint(
                condition=Q(is_deleted=False) & ~Q(route=""),
                fields=("company", "route"),
                name="agents_agent_route_per_company",
            ),
        ),
    ]
