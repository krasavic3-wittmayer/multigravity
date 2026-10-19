"""Integration tests for Dispatcher, fault tolerance, and acceptance criteria."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from multigravity.adapters.fake import FakeWorkerAdapter, WorkerFaultRule
from multigravity.config import SystemConfig, WorkerConfig
from multigravity.dispatcher import Dispatcher
from multigravity.journal import JournalStore
from multigravity.logging import StructuredLogger
from multigravity.models import ErrorCode, TaskState, WorkerState


class TestDispatcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp_dir.name) / "logs"
        self.journal_dir = Path(self.tmp_dir.name) / "journals"

        self.workers_cfg = [
            WorkerConfig(id="worker-1", label="Worker 1", profile_dir="~/.multigravity/workers/w1"),
            WorkerConfig(id="worker-2", label="Worker 2", profile_dir="~/.multigravity/workers/w2"),
            WorkerConfig(id="worker-3", label="Worker 3", profile_dir="~/.multigravity/workers/w3"),
        ]

        self.config = SystemConfig(
            cooldown_base_s=0.2,
            cooldown_cap_s=1.0,
            queue_deadline_s=1.5,
            task_timeout_s=2.0,
            max_attempts=3,
            default_max_output_chars=4000,
            log_dir=str(self.log_dir),
            journal_dir=str(self.journal_dir),
            workers=self.workers_cfg,
        )

        from multigravity.workers import WorkerRegistry

        self.registry = WorkerRegistry(self.workers_cfg)
        self.adapter = FakeWorkerAdapter()
        self.journal = JournalStore(self.journal_dir)
        self.logger = StructuredLogger(self.log_dir)

        self.dispatcher = Dispatcher(
            config=self.config,
            registry=self.registry,
            adapter=self.adapter,
            journal=self.journal,
            logger=self.logger,
        )
        await self.dispatcher.start()

    async def asyncTearDown(self):
        await self.dispatcher.stop()
        self.tmp_dir.cleanup()

    async def test_criterion_1_parallel_execution(self):
        """Acceptance Criterion 1: With 3 mock workers, 10 submitted tasks complete
        in roughly parallel time, not serial time."""
        # Each task sleeps 0.1s
        for w in self.workers_cfg:
            self.adapter.inject_latency(w.id, 0.1)

        t0 = time.perf_counter()
        task_ids = [
            self.dispatcher.submit_task(instruction=f"Task {i}") for i in range(10)
        ]

        # Wait for all to complete
        statuses = await self.dispatcher.wait(task_ids, timeout_s=10.0)
        t_elapsed = time.perf_counter() - t0

        self.assertTrue(all(s["state"] == TaskState.DONE.value for s in statuses))
        # Serial time would be >= 1.0s (10 * 0.1s)
        # With 3 workers parallel: ceil(10/3) * 0.1s = ~0.4s
        self.assertLess(t_elapsed, 0.8, f"Elapsed time {t_elapsed:.2f}s took longer than parallel expectation")

    async def test_criterion_2_rate_limit_failover(self):
        """Acceptance Criterion 2: Injecting 429 on a busy worker: the task finishes
        on another worker, main-agent API returns no error, and failed worker
        re-enters rotation after cooldown."""
        # Inject 429 on worker-1 for 1 attempt with a very short cooldown
        self.adapter.inject_rate_limit("worker-1", retry_after_s=0.2, count=1)

        task_id = self.dispatcher.submit_task(instruction="Test 429 Failover")

        # Wait for task completion
        await self.dispatcher.wait([task_id], timeout_s=5.0)

        result_payload = self.dispatcher.get_result(task_id)
        self.assertEqual(result_payload.get("status"), TaskState.DONE.value)
        self.assertIn("result", result_payload)
        self.assertNotIn("error", result_payload)

        # Confirm task completed with attempt > 1 and worker-1 was tried
        task = self.journal.get_task(task_id)
        self.assertGreater(task.attempt, 1)
        self.assertIn("worker-1", task.workers_tried)

        # Wait for cooldown to expire and verify worker-1 re-enters rotation
        await asyncio.sleep(0.3)
        workers = self.dispatcher.list_workers()
        w1_status = next(w for w in workers if w["id"] == "worker-1")
        self.assertEqual(w1_status["state"], WorkerState.IDLE.value)

    async def test_criterion_3_all_workers_cooldown_deadline(self):
        """Acceptance Criterion 3: All workers in cooldown: tasks wait, then fail
        cleanly with NO_WORKER_AVAILABLE after deadline; none are lost or looped."""
        # Force all 3 workers into high cooldown
        for w in self.workers_cfg:
            self.registry.mark_cooldown(w.id, base_s=10.0, cap_s=10.0, retry_after_s=10.0)

        task_id = self.dispatcher.submit_task(instruction="Task blocked by cooldown")

        # Wait until deadline passes (queue_deadline_s is 1.5s)
        await asyncio.sleep(1.8)

        status = self.dispatcher.get_status([task_id])[0]
        self.assertEqual(status["state"], TaskState.FAILED.value)
        self.assertEqual(status["error"]["code"], ErrorCode.NO_WORKER_AVAILABLE.value)

    async def test_criterion_5_output_contract_violation(self):
        """Acceptance Criterion 5: Oversized or malformed worker output never reaches
        the main agent and fails with OUTPUT_CONTRACT_VIOLATION."""
        # Inject malformed non-JSON output on all workers
        for w in self.workers_cfg:
            self.adapter.inject_malformed_output(w.id, "COMPLETELY_INVALID_GARBAGE_OUTPUT")

        task_id = self.dispatcher.submit_task(instruction="Malformed output test")
        await self.dispatcher.wait([task_id], timeout_s=5.0)

        result_payload = self.dispatcher.get_result(task_id)
        self.assertEqual(result_payload.get("status"), TaskState.FAILED.value)
        self.assertEqual(
            result_payload.get("error", {}).get("code"),
            ErrorCode.OUTPUT_CONTRACT_VIOLATION.value,
        )

    async def test_criterion_6_wait_timeout_cap(self):
        """Acceptance Criterion 6: wait never exceeds its timeout cap."""
        # Submitting task that will hang
        for w in self.workers_cfg:
            self.adapter.inject_hang(w.id)

        task_id = self.dispatcher.submit_task(instruction="Hanging task")

        t0 = time.perf_counter()
        # Request wait of 0.2s
        await self.dispatcher.wait([task_id], timeout_s=0.2)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 0.5)

    async def test_auth_expired_handling(self):
        """Worker experiencing auth failure is marked AUTH_EXPIRED and task fails over."""
        self.adapter.inject_auth_failure("worker-1")

        task_id = self.dispatcher.submit_task(instruction="Auth failure test")
        await self.dispatcher.wait([task_id], timeout_s=5.0)

        result = self.dispatcher.get_result(task_id)
        self.assertEqual(result.get("status"), TaskState.DONE.value)

        # worker-1 must be in AUTH_EXPIRED state
        workers = self.dispatcher.list_workers()
        w1_status = next(w for w in workers if w["id"] == "worker-1")
        self.assertEqual(w1_status["state"], WorkerState.AUTH_EXPIRED.value)

    async def test_task_cancellation(self):
        """Cancelling a task stops execution and marks it CANCELLED."""
        for w in self.workers_cfg:
            self.adapter.inject_hang(w.id)

        task_id = self.dispatcher.submit_task(instruction="To be cancelled")
        await asyncio.sleep(0.1)

        cancelled = await self.dispatcher.cancel_task(task_id)
        self.assertTrue(cancelled)

        status = self.dispatcher.get_status([task_id])[0]
        self.assertEqual(status["state"], TaskState.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
