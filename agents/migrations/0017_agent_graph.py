"""``Agent.graph``: the runnable flow as graph JSON (what chat runs).

Filled here from each agent's reachable Steps; afterwards the agents signals
rewrite it whenever a Step, a Step's tools or a Tool changes.
"""

from django.db import migrations, models


def fill_graphs(apps, schema_editor):
    from agents.services.graph_json import graph_from_chain, step_dicts, walk_chain

    Agent = apps.get_model("agents", "Agent")
    Step = apps.get_model("agents", "Step")
    for agent in Agent._base_manager.all():
        steps = step_dicts(Step._base_manager.filter(agent=agent, is_deleted=False))
        Agent._base_manager.filter(pk=agent.pk).update(
            graph=graph_from_chain(agent.slug, walk_chain(steps))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0016_tool_calling_tools"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="graph",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(fill_graphs, migrations.RunPython.noop),
    ]
