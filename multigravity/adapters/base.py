"""Abstract base class for worker execution adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class WorkerResult:
    success: bool
    raw_output: str = ""
    error_code: str | None = None
    error_message: str | None = None
    retry_after_s: float | None = None


class WorkerAdapter(ABC):
    """Abstract interface that every worker adapter must implement."""

    adapter_kind: str = "base"

    @abstractmethod
    async def run(
        self,
        worker_id: str,
        prompt: str,
        timeout_s: float,
        working_dir: str | None = None,
        profile_dir: str | None = None,
        inputs: Any = None,
        mode: str | None = None,
    ) -> WorkerResult:
        """Executes a prompt on the given worker within timeout_s."""
        pass

    @abstractmethod
    async def cancel(self, worker_id: str):
        """Cancels any in-flight execution for worker_id."""
        pass

    @abstractmethod
    async def probe(self, worker_id: str) -> bool:
        """Cheaply verifies if the worker is reachable and authenticated."""
        pass
