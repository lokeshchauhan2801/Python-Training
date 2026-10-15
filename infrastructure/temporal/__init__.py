"""Temporal workflow transport. The workflow module uses module-level
``@activity.defn``/``@workflow.defn`` decorators (the only import-time temporalio
touch); all Django/ORM imports stay inside the activity body."""
