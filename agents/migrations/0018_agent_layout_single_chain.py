"""``Agent.layout`` (where the canvas draws START/END), and one straight flow.

Older canvases let several steps be wired from START and to END at once, but
the runtime only ever ran the first of them. Here every agent's extra entry
steps are appended, in canvas order, after the end of its main chain, so each
agent has exactly one entry step and one final step. ``Agent.graph`` is then
rebuilt from the steps.
"""

from django.db import migrations, models


def single_chain(apps, schema_editor):
    from agents.services.graph_json import graph_from_chain, step_dicts, walk_chain

    Agent = apps.get_model("agents", "Agent")
    Step = apps.get_model("agents", "Step")
    for agent in Agent._base_manager.filter(is_deleted=False):
        live = Step._base_manager.filter(agent=agent, is_deleted=False)
        chain = walk_chain(step_dicts(live))
        if chain:
            in_chain = [s["pk"] for s in chain]
            extras = list(
                live.filter(connected_from_start=True)
                .exclude(pk__in=in_chain)
                .order_by("order", "created_at")
            )
            # Detach the extras' own wiring, then line them up after the tail.
            order = [Step._base_manager.get(pk=pk) for pk in in_chain] + extras
            for i, step in enumerate(order):
                step.connected_from_start = i == 0
                if i + 1 < len(order):
                    step.next_step = order[i + 1]
                    step.connected_to_end = False
                else:
                    step.next_step = None
                    step.connected_to_end = True
                step.save(update_fields=["connected_from_start", "connected_to_end", "next_step"])
            # No other step keeps a START/END flag.
            live.exclude(pk__in=[s.pk for s in order]).update(
                connected_from_start=False, connected_to_end=False
            )
        steps = step_dicts(Step._base_manager.filter(agent=agent, is_deleted=False))
        Agent._base_manager.filter(pk=agent.pk).update(
            graph=graph_from_chain(agent.slug, walk_chain(steps))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0017_agent_graph"),
    ]

    operations = [
        migrations.AddField(
            model_name="agent",
            name="layout",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(single_chain, migrations.RunPython.noop),
    ]
