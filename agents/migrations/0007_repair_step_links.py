"""Repair step links left inconsistent by the old START/END checkboxes.

* A step wired to a next step AND to END stopped the flow before the next
  step. The explicit next hop wins: its END link is cleared.
* An agent with several entry steps keeps only the one the runtime walker
  uses (lowest ``order``, then oldest).
"""

from django.db import migrations


def repair(apps, schema_editor):
    Step = apps.get_model("agents", "Step")
    live = Step.objects.filter(is_deleted=False)

    for step in live.filter(connected_to_end=True, next_step__isnull=False):
        nxt = live.filter(pk=step.next_step_id, agent_id=step.agent_id).first()
        if nxt is not None:
            step.connected_to_end = False
            step.save(update_fields=["connected_to_end"])

    agent_ids = live.filter(connected_from_start=True).values_list("agent_id", flat=True)
    for agent_id in set(agent_ids):
        entries = list(
            live.filter(agent_id=agent_id, connected_from_start=True).order_by(
                "order", "created_at"
            )
        )
        for extra in entries[1:]:
            extra.connected_from_start = False
            extra.save(update_fields=["connected_from_start"])


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0006_step_delegate_agent"),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
