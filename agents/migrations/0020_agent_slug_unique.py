"""Make every Agent slug unique across all companies.

The slug is the agent's chat route (``/chat/<session>/send/<slug>/``), so two
live agents may never share one, whatever their company (or global).
"""

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("agents", "0019_tool_docs_process_destination"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="agent",
            name="agents_agent_slug_per_company",
        ),
        migrations.AddConstraint(
            model_name="agent",
            constraint=models.UniqueConstraint(
                condition=Q(is_deleted=False),
                fields=("slug",),
                name="agents_agent_slug_unique",
            ),
        ),
    ]
