"""Run the Celery worker for document processing.

Same as ``celery -A base.celery worker -Q document-processing,celery``, as a
management command so ``start_processing.sh`` can run and track it like the
Kafka and Temporal workers. Needs Redis at ``REDIS_URL``.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Celery worker for document processing (Redis broker)."

    def add_arguments(self, parser):
        parser.add_argument("--concurrency", type=int, default=None)
        parser.add_argument("--loglevel", default="info")

    def handle(self, *args, **options):
        from django.conf import settings

        from base.celery import app

        argv = [
            "worker",
            "--queues=document-processing,celery",
            f"--loglevel={options['loglevel']}",
            f"--concurrency={options['concurrency'] or settings.CELERY_WORKER_CONCURRENCY}",
            "--hostname=documents@%h",
        ]
        self.stdout.write(
            f"Starting Celery worker on queue 'document-processing' "
            f"(broker {settings.CELERY_BROKER_URL})"
        )
        app.worker_main(argv)
