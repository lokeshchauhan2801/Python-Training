from .interfaces import ProcessingBackend, ProcessingRequest


class KafkaProcessingBackend(ProcessingBackend):
    TOPIC = "document.processing.requested"

    def dispatch(self, request: ProcessingRequest) -> str:
        from infrastructure.kafka.interfaces import DomainEvent
        from infrastructure.kafka.producer import KafkaEventPublisher

        event = DomainEvent(
            event_type="document.processing.requested",
            company_id=request.company_id,
            user_id=request.user_id,
            resource_id=request.document_id,
            payload={
                "backend": request.backend,
                "surface": (request.extra or {}).get("surface", "documents"),
            },
        )
        publisher = KafkaEventPublisher()
        publisher.publish(self.TOPIC, event)
        return event.event_id

    def get_status(self, job_id: str) -> str:
        # Kafka is fire-and-forget from the dispatcher's perspective.
        return "PUBLISHED"
