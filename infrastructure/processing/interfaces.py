"""Processing backend strategy interface."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ProcessingRequest:
    document_id: str
    company_id: str
    user_id: str
    backend: str
    extra: dict[str, Any] | None = None


class ProcessingBackend(ABC):
    @abstractmethod
    def dispatch(self, request: ProcessingRequest) -> str:
        """Dispatch a processing request. Return a job/task ID."""

    @abstractmethod
    def get_status(self, job_id: str) -> str:
        """Return current job status."""
