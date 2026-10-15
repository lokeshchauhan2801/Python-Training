"""Celery application for agentic-flow.

Importing this module constructs the Celery app object but makes NO broker
connection — that happens lazily on the first ``.delay()``/``apply_async()`` or
when a worker boots. The app is wired into ``base/__init__.py`` so Django and
``shared_task`` discovery pick it up. All CELERY_* settings are read from Django
settings under the ``CELERY`` namespace.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "base.settings")

app = Celery("agentic-flow")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
