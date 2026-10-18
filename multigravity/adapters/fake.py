"""FakeWorker adapter for testing with configurable fault injection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from multigravity.adapters.base import WorkerAdapter, WorkerResult
from multigravity.models import ErrorCode, OutputContract


@dataclass
class WorkerFaultRule:
    latency_s: float = 0.0
    rate_limit_count: int = 0
    retry_after_s: float | None = None
    auth_failure: bool = False
    hang: bool = False
    malformed_output: str | None = None
    oversized_length: int | None = None


class FakeWorkerAdapter(WorkerAdapter):
    """Mock worker adapter for unit testing, concurrency verification, and fault recovery."""

    adapter_kind: str = "fake"

    def __init__(self):
        self.rules: dict[str, WorkerFaultRule] = {}
        self.running_tasks: dict[str, asyncio.Task] = {}
        self.execution_counts: dict[str, int] = {}

    def set_rule(self, worker_id: str, rule: WorkerFaultRule):
        self.rules[worker_id] = rule

    def inject_latency(self, worker_id: str, latency_s: float):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.latency_s = latency_s

    def inject_rate_limit(self, worker_id: str, retry_after_s: float = 2.0, count: int = 1):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.rate_limit_count = count
        rule.retry_after_s = retry_after_s

    def inject_auth_failure(self, worker_id: str):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.auth_failure = True

    def inject_hang(self, worker_id: str):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.hang = True

    def inject_malformed_output(self, worker_id: str, text: str = "INVALID_NON_JSON"):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.malformed_output = text

    def inject_oversized_output(self, worker_id: str, length: int = 10000):
        rule = self.rules.setdefault(worker_id, WorkerFaultRule())
        rule.oversized_length = length

    def clear_rules(self, worker_id: str | None = None):
        if worker_id:
            self.rules.pop(worker_id, None)
        else:
            self.rules.clear()

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
        self.execution_counts[worker_id] = self.execution_counts.get(worker_id, 0) + 1
        rule = self.rules.get(worker_id, WorkerFaultRule())

        coro = self._execute_with_faults(worker_id, prompt, rule)
        task = asyncio.create_task(coro)
        self.running_tasks[worker_id] = task

        try:
            return await asyncio.wait_for(task, timeout=timeout_s)
        except asyncio.TimeoutError:
            return WorkerResult(
                success=False,
                error_code=ErrorCode.TIMEOUT.value,
                error_message=f"Execution timed out after {timeout_s}s",
            )
        except asyncio.CancelledError:
            return WorkerResult(
                success=False,
                error_code=ErrorCode.CANCELLED.value,
                error_message="Worker task was cancelled",
            )
        finally:
            self.running_tasks.pop(worker_id, None)

    async def _execute_with_faults(
        self, worker_id: str, prompt: str, rule: WorkerFaultRule
    ) -> WorkerResult:
        if rule.hang:
            # Sleep indefinitely until cancelled or timed out
            await asyncio.sleep(86400)

        if rule.latency_s > 0:
            await asyncio.sleep(rule.latency_s)

        if rule.auth_failure:
            return WorkerResult(
                success=False,
                error_code=ErrorCode.AUTH_EXPIRED.value,
                error_message="oauth2: cannot fetch token: 401 Unauthorized",
            )

        if rule.rate_limit_count > 0:
            rule.rate_limit_count -= 1
            return WorkerResult(
                success=False,
                error_code=ErrorCode.RATE_LIMITED.value,
                error_message="HTTP 429: Resource exhausted / rate limit exceeded",
                retry_after_s=rule.retry_after_s,
            )

        if rule.malformed_output is not None:
            return WorkerResult(
                success=True,
                raw_output=rule.malformed_output,
            )

        if rule.oversized_length is not None:
            contract = OutputContract(
                summary="[FAKE] " + "A" * rule.oversized_length,
                key_findings=["Finding 1"],
            )
            return WorkerResult(
                success=True,
                raw_output=json.dumps(contract.to_dict()),
            )

        # Default valid response
        contract = OutputContract(
            summary=f"[FAKE] Task executed successfully by {worker_id}",
            key_findings=["Worker executed prompt", "Contract schema verified"],
            confidence=0.99,
        )
        return WorkerResult(
            success=True,
            raw_output=json.dumps(contract.to_dict()),
        )

    async def cancel(self, worker_id: str):
        task = self.running_tasks.get(worker_id)
        if task and not task.done():
            task.cancel()

    async def probe(self, worker_id: str) -> bool:
        rule = self.rules.get(worker_id)
        if rule and rule.auth_failure:
            return False
        return True
