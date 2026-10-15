"""Temporal workflow client abstraction."""
from abc import ABC, abstractmethod
from typing import Any


class WorkflowClient(ABC):
    @abstractmethod
    async def start_workflow(
        self, workflow_type: str, args: dict[str, Any], task_queue: str
    ) -> str:
        """Start a workflow. Return workflow run ID."""

    @abstractmethod
    async def get_workflow_status(self, workflow_id: str) -> str:
        """Return workflow execution status."""
