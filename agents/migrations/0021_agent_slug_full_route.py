"""Store each Agent's complete chat route in its slug.

``chat_agent`` becomes ``/chat/chat_agent/``: a POST to that path is answered
by the agent. The Langfuse prompt keeps the name part (``chat_agent``).
"""

from django.db import migrations, models


def to_full_route(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    for agent in Agent._base_manager.all():
        name = (agent.slug or "").strip()
        if name.startswith("/chat/"):
            name = name[len("/chat/"):]
        name = name.strip("/")
        Agent._base_manager.filter(pk=agent.pk).update(slug=f"/chat/{name}/")


def to_name(apps, schema_editor):
    Agent = apps.get_model("agents", "Agent")
    for agent in Agent._base_manager.all():
        name = (agent.slug or "").strip()
        if name.startswith("/chat/"):
            name = name[len("/chat/"):]
        Agent._base_manager.filter(pk=agent.pk).update(slug=name.strip("/"))


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0020_agent_slug_unique"),
    ]

    operations = [
        migrations.AlterField(
            model_name="agent",
            name="slug",
            field=models.CharField(max_length=80),
        ),
        migrations.RunPython(to_full_route, to_name),
    ]
