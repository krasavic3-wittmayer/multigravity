"""Task queue, FIFO dispatcher, fault recovery, and execution engine."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from multigravity.adapters.base import WorkerAdapter, WorkerResult
from multigravity.config import SystemConfig
from multigravity.journal import JournalStore
from multigravity.logging import StructuredLogger
from multigravity.models import (
    ErrorCode,
    OutputContract,
    StructuredError,
    Task,
    TaskKind,
    TaskMode,
    TaskState,
)
from multigravity.validator import OutputContractValidator
from multigravity.workers import WorkerRegistry


class Dispatcher:
    """Core task dispatcher and queue manager."""

    def __init__(
        self,
        config: SystemConfig,
        registry: WorkerRegistry,
        adapter: WorkerAdapter,
        journal: JournalStore,
        logger: StructuredLogger,
    ):
        self.config = config
        self.registry = registry
        self.adapter = adapter
        self.journal = journal
        self.logger = logger

        # Queue: list of task_ids waiting to be assigned (FIFO)
        self._queue: list[str] = []
        # Active in-flight worker tasks: worker_id -> asyncio.Task
        self._active_worker_tasks: dict[str, asyncio.Task] = {}
        # Task ID to worker ID mapping
        self._task_worker_map: dict[str, str] = {}
        # Async condition variable to wake dispatcher loop when new tasks arrive or workers become free
        self._wakeup_event = asyncio.Event()
        self._running = False
        self._dispatch_loop_task: asyncio.Task | None = None

    async def start(self):
        """Starts the background dispatcher loop and recovers journal state."""
        self._running = True
        # Recover tasks from journal
        recovered = self.journal.recover()
        for task in recovered.values():
            if task.state == TaskState.QUEUED:
                if task.task_id not in self._queue:
                    self._queue.append(task.task_id)

        self._dispatch_loop_task = asyncio.create_task(self._run_dispatch_loop())

    async def stop(self):
        """Gracefully shuts down the dispatcher and cancels in-flight executions."""
        self._running = False
        self._wakeup_event.set()

        if self._dispatch_loop_task and not self._dispatch_loop_task.done():
            self._dispatch_loop_task.cancel()
            try:
                await self._dispatch_loop_task
            except asyncio.CancelledError:
                pass

        # Cancel all in-flight worker tasks
        for worker_id, task in list(self._active_worker_tasks.items()):
            await self.adapter.cancel(worker_id)
            if not task.done():
                task.cancel()

        self._active_worker_tasks.clear()
        self._task_worker_map.clear()

    def submit_task(
        self,
        kind: str = TaskKind.RESEARCH.value,
        instruction: str = "",
        inputs: Any = None,
        output_spec: dict[str, Any] | None = None,
        max_output_chars: int | None = None,
        mode: str = TaskMode.READ_ONLY.value,
    ) -> str:
        """Enqueues a new task and returns its task_id."""
        task_id = str(uuid.uuid4())
        max_chars = max_output_chars or self.config.default_max_output_chars

        now = time.time()
        task = Task(
            task_id=task_id,
            kind=kind,
            instruction=instruction,
            inputs=inputs or {},
            output_spec=output_spec,
            max_output_chars=max_chars,
            mode=mode,
            state=TaskState.QUEUED,
            created_at=now,
            enqueued_at=now,
        )

        self.journal.save_task(task)
        self._queue.append(task_id)
        self.logger.task_submitted(task_id, kind, mode, max_chars)
        self._wakeup_event.set()
        return task_id

    def get_status(self, task_ids: list[str]) -> list[dict[str, Any]]:
        """Returns per-task state, attempt count, worker label, timestamps."""
        results: list[dict[str, Any]] = []
        for tid in task_ids:
            task = self.journal.get_task(tid)
            if not task:
                results.append({"task_id": tid, "state": "UNKNOWN", "error": "Task not found"})
                continue

            item: dict[str, Any] = {
                "task_id": task.task_id,
                "state": task.state.value,
                "attempt": task.attempt,
                "worker_label": task.worker_label,
                "created_at": task.created_at,
                "assigned_at": task.assigned_at,
                "completed_at": task.completed_at,
            }
            if task.error:
                item["error"] = task.error.to_dict()
            results.append(item)
        return results

    def get_result(self, task_id: str) -> dict[str, Any]:
        """Returns compressed result only, or structured error."""
        task = self.journal.get_task(task_id)
        if not task:
            return {"error": StructuredError(code=ErrorCode.TASK_FAILED.value, message=f"Task {task_id} not found").to_dict()}

        if task.state == TaskState.DONE:
            return {"task_id": task.task_id, "status": "DONE", "result": task.result}
        elif task.state in (TaskState.FAILED, TaskState.CANCELLED):
            err_dict = task.error.to_dict() if task.error else {"code": task.state.value, "message": "Task ended with failure"}
            return {"task_id": task.task_id, "status": task.state.value, "error": err_dict}
        else:
            return {"task_id": task.task_id, "status": task.state.value, "message": "Result not yet available"}

    async def wait(self, task_ids: list[str], timeout_s: float = 25.0) -> list[dict[str, Any]]:
        """Blocks until any/all tasks finish or timeout hits. Hard-capped at 25s."""
        # Enforce hard-cap so client never times out
        capped_timeout = min(float(timeout_s), 25.0)
        deadline = time.time() + capped_timeout

        while time.time() < deadline:
            statuses = self.get_status(task_ids)
            # Check if all requested tasks have finished
            all_done = all(s.get("state") in (TaskState.DONE.value, TaskState.FAILED.value, TaskState.CANCELLED.value) for s in statuses)
            if all_done:
                return statuses

            wait_remaining = max(0.05, min(0.5, deadline - time.time()))
            await asyncio.sleep(wait_remaining)

        return self.get_status(task_ids)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancels an in-flight or queued task."""
        task = self.journal.get_task(task_id)
        if not task:
            return False

        if task.state in (TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED):
            return True

        # Remove from queue if still queued
        if task_id in self._queue:
            self._queue.remove(task_id)

        # Cancel running worker if assigned
        worker_id = self._task_worker_map.get(task_id)
        if worker_id:
            await self.adapter.cancel(worker_id)
            w_task = self._active_worker_tasks.get(worker_id)
            if w_task and not w_task.done():
                w_task.cancel()

        task.state = TaskState.CANCELLED
        task.completed_at = time.time()
        task.error = StructuredError(
            code=ErrorCode.CANCELLED.value,
            message="Task was explicitly cancelled",
            retryable=False,
        )
        self.journal.save_task(task)
        self.logger.task_cancelled(task_id)
        self._wakeup_event.set()
        return True

    def list_workers(self) -> list[dict[str, Any]]:
        workers = self.registry.list_workers()
        adapter_kind = getattr(self.adapter, "adapter_kind", "unknown")
        for w in workers:
            w["adapter"] = adapter_kind
        return workers

    async def _run_dispatch_loop(self):
        """Continuously dispatches queued tasks to idle workers."""
        while self._running:
            self._wakeup_event.clear()

            # Process queue
            i = 0
            while i < len(self._queue):
                task_id = self._queue[i]
                task = self.journal.get_task(task_id)
                if not task:
                    self._queue.pop(i)
                    continue

                # Check queue deadline
                now = time.time()
                queue_start = task.enqueued_at if task.enqueued_at is not None else task.created_at
                if (now - queue_start) > self.config.queue_deadline_s:
                    task.state = TaskState.FAILED
                    task.completed_at = now
                    task.error = StructuredError(
                        code=ErrorCode.NO_WORKER_AVAILABLE.value,
                        message=f"Queue deadline ({self.config.queue_deadline_s}s) exceeded with no available worker",
                        retryable=False,
                    )
                    self.journal.save_task(task)
                    self.logger.task_failed(task.task_id, task.error.code, task.error.message)
                    self._queue.pop(i)
                    continue

                # Find available worker (excluding workers already failed on this attempt)
                worker = self.registry.get_least_recently_used_idle(excluded_worker_ids=task.workers_tried)
                if not worker and task.workers_tried:
                    # If all idle workers were excluded but some are now IDLE (e.g. cooldown passed), allow retry
                    worker = self.registry.get_least_recently_used_idle(excluded_worker_ids=[])
                    if worker:
                        task.workers_tried.clear()

                if not worker:
                    # Check if all workers are permanently dead or disabled
                    if not self.registry.any_available_or_cooldown():
                        task.state = TaskState.FAILED
                        task.completed_at = now
                        task.error = StructuredError(
                            code=ErrorCode.NO_WORKER_AVAILABLE.value,
                            message="All worker profiles are disabled or in AUTH_EXPIRED state",
                            retryable=False,
                        )
                        self.journal.save_task(task)
                        self.logger.task_failed(task.task_id, task.error.code, task.error.message)
                        self._queue.pop(i)
                        continue
                    # Otherwise workers might be busy or in cooldown; move to next task in queue
                    i += 1
                    continue

                # Worker found! Assign task
                self._queue.pop(i)
                self._assign_and_launch(task, worker.id, worker.label)

            # Wait for wakeup event or brief timeout
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    def _assign_and_launch(self, task: Task, worker_id: str, worker_label: str):
        now = time.time()
        task.state = TaskState.RUNNING
        task.assigned_worker_id = worker_id
        task.worker_label = worker_label
        task.assigned_at = now
        task.enqueued_at = None
        self.journal.save_task(task)
        self.registry.mark_busy(worker_id)
        self.logger.task_assigned(
            task.task_id,
            worker_id,
            worker_label,
            task.attempt,
            adapter=getattr(self.adapter, "adapter_kind", "unknown"),
        )

        self._task_worker_map[task.task_id] = worker_id
        worker_task = asyncio.create_task(self._execute_task_on_worker(task, worker_id))
        self._active_worker_tasks[worker_id] = worker_task

    async def _execute_task_on_worker(self, task: Task, worker_id: str):
        try:
            # Wrap prompt with contract
            prompt = OutputContractValidator.wrap_prompt(
                kind=task.kind,
                instruction=task.instruction,
                inputs=task.inputs,
                max_output_chars=task.max_output_chars,
            )

            # Execute via adapter
            worker = self.registry.get_worker(worker_id)
            p_dir = worker.profile_dir if worker else None
            worker_res: WorkerResult = await self.adapter.run(
                worker_id=worker_id,
                prompt=prompt,
                timeout_s=self.config.task_timeout_s,
                profile_dir=p_dir,
                inputs=task.inputs,
                mode=task.mode,
            )

            await self._handle_worker_result(task, worker_id, worker_res)

        except asyncio.CancelledError:
            # Task or worker was cancelled
            pass
        except Exception as e:
            # Unexpected exception during execution
            await self._handle_worker_failure(
                task, worker_id, ErrorCode.TASK_FAILED.value, str(e), is_rate_limit=False, is_auth_failure=False
            )
        finally:
            self._active_worker_tasks.pop(worker_id, None)
            self._task_worker_map.pop(task.task_id, None)
            self._wakeup_event.set()

    async def _handle_worker_result(self, task: Task, worker_id: str, res: WorkerResult):
        # 1. Check for rate limit
        if res.error_code == ErrorCode.RATE_LIMITED.value:
            duration = self.registry.mark_cooldown(
                worker_id,
                base_s=self.config.cooldown_base_s,
                cap_s=self.config.cooldown_cap_s,
                retry_after_s=res.retry_after_s,
            )
            self.logger.worker_cooldown(worker_id, duration, res.error_message or "Rate limited")
            await self._retry_task(task, worker_id, ErrorCode.RATE_LIMITED.value, res.error_message or "Rate limited")
            return

        # 2. Check for auth/session expiry
        if res.error_code == ErrorCode.AUTH_EXPIRED.value:
            self.registry.mark_auth_expired(worker_id)
            self.logger.worker_auth_expired(worker_id, res.error_message or "Auth expired")
            await self._retry_task(task, worker_id, ErrorCode.AUTH_EXPIRED.value, res.error_message or "Auth expired")
            return

        # 3. Check for timeout / hang
        if res.error_code == ErrorCode.TIMEOUT.value:
            self.registry.mark_idle(worker_id)
            await self._retry_task(task, worker_id, ErrorCode.TIMEOUT.value, res.error_message or "Task timeout")
            return

        # 4. Check for cancellation
        if res.error_code == ErrorCode.CANCELLED.value:
            self.registry.mark_idle(worker_id)
            return

        # 5. Check other provider or adapter errors
        if not res.success:
            self.registry.mark_idle(worker_id)
            await self._retry_task(
                task, worker_id, res.error_code or ErrorCode.TASK_FAILED.value, res.error_message or "Execution failed"
            )
            return

        # 6. Save raw transcript on disk (never returned to main agent)
        transcript_path = OutputContractValidator.save_raw_transcript(
            task.task_id, self.config.log_dir, res.raw_output
        )
        task.raw_transcript_path = transcript_path

        # 7. Validate output contract
        is_valid, contract, violation_reason = OutputContractValidator.validate_and_parse(
            res.raw_output, task.max_output_chars
        )

        if not is_valid:
            # One automatic repair/compress pass per SPEC section 7
            repaired_contract = await self._attempt_repair_pass(task, worker_id, res.raw_output, violation_reason or "Schema mismatch")
            if repaired_contract:
                contract = repaired_contract
                is_valid = True
            else:
                self.registry.mark_idle(worker_id)
                task.state = TaskState.FAILED
                task.completed_at = time.time()
                task.error = StructuredError(
                    code=ErrorCode.OUTPUT_CONTRACT_VIOLATION.value,
                    message=f"Output violated contract schema: {violation_reason}",
                    retryable=False,
                )
                self.journal.save_task(task)
                self.logger.task_failed(task.task_id, task.error.code, task.error.message)
                return

        # Success!
        self.registry.mark_idle(worker_id)
        task.state = TaskState.DONE
        task.completed_at = time.time()
        task.result = contract.to_dict()
        task.error = None
        self.journal.save_task(task)
        self.logger.task_done(
            task.task_id,
            task.completed_at - (task.assigned_at or task.created_at),
            adapter=getattr(self.adapter, "adapter_kind", "unknown"),
        )

    async def _attempt_repair_pass(
        self, task: Task, worker_id: str, previous_output: str, reason: str
    ) -> OutputContract | None:
        repair_prompt = OutputContractValidator.build_repair_prompt(
            previous_output, reason, task.max_output_chars
        )
        worker = self.registry.get_worker(worker_id)
        p_dir = worker.profile_dir if worker else None
        repair_res = await self.adapter.run(
            worker_id=worker_id,
            prompt=repair_prompt,
            timeout_s=self.config.task_timeout_s,
            profile_dir=p_dir,
            inputs=task.inputs,
            mode=task.mode,
        )
        if not repair_res.success:
            return None

        is_valid, contract, _ = OutputContractValidator.validate_and_parse(
            repair_res.raw_output, task.max_output_chars
        )
        return contract if is_valid else None

    async def _retry_task(self, task: Task, failed_worker_id: str, error_code: str, reason: str):
        if failed_worker_id not in task.workers_tried:
            task.workers_tried.append(failed_worker_id)

        task.attempt += 1
        if task.attempt > self.config.max_attempts:
            # Exhausted max attempts
            task.state = TaskState.FAILED
            task.completed_at = time.time()
            task.error = StructuredError(
                code=ErrorCode.TASK_FAILED.value,
                message=f"Task failed after {self.config.max_attempts} attempts. Last error: {reason}",
                retryable=False,
            )
            self.journal.save_task(task)
            self.logger.task_failed(task.task_id, task.error.code, task.error.message)
            return

        # Re-queue at the FRONT per SPEC section 6
        task.state = TaskState.QUEUED
        task.assigned_worker_id = None
        task.worker_label = None
        task.enqueued_at = time.time()
        self.journal.save_task(task)
        self.logger.task_retried(task.task_id, task.attempt, error_code, reason)
        self._queue.insert(0, task.task_id)

    async def _handle_worker_failure(
        self,
        task: Task,
        worker_id: str,
        code: str,
        message: str,
        is_rate_limit: bool = False,
        is_auth_failure: bool = False,
    ):
        if is_rate_limit:
            dur = self.registry.mark_cooldown(worker_id, self.config.cooldown_base_s, self.config.cooldown_cap_s)
            self.logger.worker_cooldown(worker_id, dur, message)
        elif is_auth_failure:
            self.registry.mark_auth_expired(worker_id)
            self.logger.worker_auth_expired(worker_id, message)
        else:
            self.registry.mark_idle(worker_id)

        await self._retry_task(task, worker_id, code, message)
