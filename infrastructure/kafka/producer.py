"""Kafka event producer."""
from __future__ import annotations

import json
import logging

from .interfaces import DomainEvent, EventPublisher

logger = logging.getLogger(__name__)


class KafkaEventPublisher(EventPublisher):
    def __init__(self, bootstrap_servers: str | None = None):
        from django.conf import settings

        self._servers = bootstrap_servers or getattr(
            settings, "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        self._producer = None

    @property
    def producer(self):
        if self._producer is None:
            from confluent_kafka import Producer

            self._producer = Producer({"bootstrap.servers": self._servers})
        return self._producer

    def publish(self, topic: str, event: DomainEvent) -> None:
        payload = json.dumps(
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "company_id": event.company_id,
                "user_id": event.user_id,
                "resource_id": event.resource_id,
                "payload": event.payload,
            }
        ).encode()
        self.producer.produce(topic, value=payload, callback=self._delivery_callback)


        self.producer.flush(timeout=10)

    @staticmethod
    def _delivery_callback(err, msg):
        if err:
            logger.error("Kafka delivery failed: %s", err)
        else:
            logger.debug("Kafka delivered to %s [%s]", msg.topic(), msg.partition())
