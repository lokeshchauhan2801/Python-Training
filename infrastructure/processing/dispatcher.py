"""ProcessingDispatcher — strategy pattern for choosing Celery/Kafka/Temporal."""
from __future__ import annotations

import logging

from .interfaces import ProcessingBackend, ProcessingRequest

logger = logging.getLogger(__name__)


class ProcessingDispatcher:
    """Routes processing requests to the configured backend strategy."""

    def __init__(self, backend: ProcessingBackend):
        self._backend = backend

    @classmethod
    def from_settings(cls) -> "ProcessingDispatcher":
        """Create dispatcher using the DOCUMENT_PROCESSING_BACKEND setting."""
        from django.conf import settings

        backend_name = getattr(
            settings, "DOCUMENT_PROCESSING_BACKEND", "celery"
        ).lower()
        backend = _load_backend(backend_name)
        return cls(backend)

    @classmethod
    def from_name(cls, name: str) -> "ProcessingDispatcher":
        """Create dispatcher for an explicitly named backend (celery/kafka/temporal)."""
        return cls(_load_backend(name.lower()))

    def dispatch(self, request: ProcessingRequest) -> str:
        logger.info(
            "Dispatching document %s via %s",
            request.document_id,
            type(self._backend).__name__,
        )
        return self._backend.dispatch(request)

    def get_status(self, job_id: str) -> str:
        return self._backend.get_status(job_id)


def _load_backend(name: str) -> ProcessingBackend:
    if name == "celery":
        from .celery_backend import CeleryProcessingBackend

        return CeleryProcessingBackend()
    if name == "kafka":
        from .kafka_backend import KafkaProcessingBackend

        return KafkaProcessingBackend()
    if name == "temporal":
        from .temporal_backend import TemporalProcessingBackend

        return TemporalProcessingBackend()
    raise ValueError(f"Unknown processing backend: {name!r}")
