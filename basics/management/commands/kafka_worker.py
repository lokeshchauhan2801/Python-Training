"""Run the Kafka document-processing consumer.

Imports of the consumer (and thus confluent_kafka) are deferred into
``handle()`` so ``--help`` never touches a broker.
"""
import signal

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Consume document-processing events from Kafka and run them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--topics",
            nargs="+",
            default=["document.processing.requested"],
            help="Kafka topics to subscribe to.",
        )
        parser.add_argument(
            "--group-id",
            default="document-processing-workers",
            help="Kafka consumer group id.",
        )

    def handle(self, *args, **options):
        from infrastructure.kafka.consumer import KafkaEventConsumer

        consumer = KafkaEventConsumer()

        def _shutdown(signum, frame):
            self.stdout.write("Shutting down Kafka consumer...")
            consumer.stop()

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        self.stdout.write(
            f"Starting Kafka consumer on topics {options['topics']} "
            f"(group {options['group_id']})"
        )
        consumer.consume(options["topics"], options["group_id"])
