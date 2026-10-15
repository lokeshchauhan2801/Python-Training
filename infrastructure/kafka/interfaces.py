"""Kafka event abstractions."""
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DomainEvent:
    event_type: str
    company_id: str
    user_id: str
    resource_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class EventPublisher(ABC):
    @abstractmethod
    def publish(self, topic: str, event: DomainEvent) -> None:
        """Publish a domain event to a topic."""


class EventConsumer(ABC):
    @abstractmethod
    def consume(self, topics: list[str], group_id: str) -> None:
        """Start consuming events from topics."""
