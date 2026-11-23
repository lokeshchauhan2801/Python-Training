"""No default processing backend.

``Document.processing_backend`` is empty until the user picks Process now,
Celery, Kafka or Temporal. Documents that were never processed only carried
the old "celery" default, so they are cleared too; every other row keeps the
backend it was really processed with.
"""

from django.db import migrations, models


def clear_unused_default(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    Document._base_manager.filter(
        status="uploaded",
        processing_backend="celery",
        processing_task_id="",
        chunk_count=0,
    ).update(processing_backend="")


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0006_document_paused_status_and_checkpoint"),
    ]

    operations = [
        migrations.AlterField(
            model_name="document",
            name="processing_backend",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.RunPython(clear_unused_default, migrations.RunPython.noop),
    ]
