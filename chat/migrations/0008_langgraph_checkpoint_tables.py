"""Create the LangGraph Postgres checkpointer's tables.

The Agent runtime keeps each chat session's thread in ``checkpoints``,
``checkpoint_blobs`` and ``checkpoint_writes`` (``chat.services.agent.checkpointer``).
``PostgresSaver.setup()`` owns that schema and its own versioning
(``checkpoint_migrations``), so this migration only calls it.
"""
from django.db import migrations


def create_checkpoint_tables(apps, schema_editor):
    from chat.services.agent.checkpointer import setup_tables

    setup_tables(schema_editor.connection.settings_dict)


class Migration(migrations.Migration):

    dependencies = [
        ("chat", "0007_conversationsession_selected_agent"),
    ]

    operations = [
        migrations.RunPython(create_checkpoint_tables, migrations.RunPython.noop),
    ]
