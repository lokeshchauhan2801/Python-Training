"""Kafka event consumer.

Reconstructs :class:`DomainEvent` records off the wire and routes them through
the shared surface router (``infrastructure.processing.handlers``). The
``handle_event`` seam is a plain function so it can be exercised directly in
tests without a live broker. All ``confluent_kafka`` imports are lazy so this
module imports cleanly wherever Django is set up.
"""
from __future__ import annotations

import json
import logging

from .interfaces import DomainEvent, EventConsumer

logger = logging.getLogger(__name__)

_VALID_SURFACES = {"documents", "scrapper"}


def handle_event(event: DomainEvent) -> dict:
    """Route a single domain event to the shared surface router.

    ``resource_id`` is the document id and is required. ``surface`` comes from
    the payload (defaulting to ``documents``) and must be a known surface.
    ``backend`` defaults to ``kafka``. Returns the router result dict.
    """
    document_id = event.resource_id
    if not document_id:
        logger.error("Skipping event %s: missing resource_id", event.event_id)
        raise ValueError("DomainEvent.resource_id is required")

    surface = event.payload.get("surface", "documents")
    if surface not in _VALID_SURFACES:
        logger.error("Skipping event %s: unknown surface %r", event.event_id, surface)
        raise ValueError(f"Unknown surface: {surface!r}")

    backend = event.payload.get("backend", "kafka")

    from infrastructure.processing.handlers import run_with_retry

    return run_with_retry(
        surface=surface,
        document_id=document_id,
        backend=backend,
        company_id=event.company_id,
        user_id=event.user_id,
    )


def handle_message(raw_bytes) -> dict | None:
    """Decode a raw Kafka message and delegate to :func:`handle_event`.

    A JSON decode error or a message missing ``event_type``/``resource_id`` is a
    bad message: it is logged and ``None`` is returned so the caller can commit
    and skip it rather than crash the loop.
    """
    try:
        data = json.loads(raw_bytes)
        event = DomainEvent(
            event_type=data["event_type"],
            company_id=data.get("company_id", ""),
            user_id=data.get("user_id", ""),
            resource_id=data["resource_id"],
            payload=data.get("payload", {}),
            event_id=data.get("event_id"),
            timestamp=data.get("timestamp"),
        )
    except (ValueError, TypeError, KeyError) as exc:
        logger.error("Discarding malformed Kafka message: %s", exc)
        return None

    return handle_event(event)


class KafkaEventConsumer(EventConsumer):
    """A confluent-kafka backed consumer with manual commits.

    One bad document never crashes the loop: handling is wrapped so the message
    is committed and skipped on failure. ``stop()`` flips the running flag so a
    signal handler can break the poll loop cleanly.
    """

    def __init__(self, bootstrap_servers=None):
        from django.conf import settings

        self._servers = bootstrap_servers or getattr(
            settings, "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        self._running = False

    def consume(self, topics, group_id) -> None:
        from confluent_kafka import Consumer, KafkaError

        consumer = Consumer(
            {
                "bootstrap.servers": self._servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        consumer.subscribe(topics)
        self._running = True
        try:
            while self._running:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    # Benign, transient conditions: end of partition, and
                    # "topic not available yet" on a fresh broker before the
                    # topic is auto-created on first publish. Keep polling.
                    if msg.error().code() in (
                        KafkaError._PARTITION_EOF,
                        KafkaError.UNKNOWN_TOPIC_OR_PART,
                    ):
                        continue
                    logger.error("Fatal Kafka consumer error: %s", msg.error())
                    raise Exception(msg.error())
                try:
                    handle_message(msg.value())
                except Exception:
                    logger.exception(
                        "Handler failed for message on %s; committing and skipping",
                        msg.topic(),
                    )
                consumer.commit(msg, asynchronous=False)
        finally:
            consumer.close()

    def stop(self) -> None:
        self._running = False
