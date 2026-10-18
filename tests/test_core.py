"""Unit tests for models, config, validator, journal, and worker registry."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from multigravity.config import SystemConfig, WorkerConfig
from multigravity.journal import JournalStore
from multigravity.models import (
    ErrorCode,
    OutputContract,
    StructuredError,
    Task,
    TaskKind,
    TaskState,
    WorkerState,
)
from multigravity.validator import OutputContractValidator
from multigravity.workers import WorkerRegistry


class TestModels(unittest.TestCase):
    def test_output_contract_serialization(self):
        contract = OutputContract(
            summary="Test summary",
            key_findings=["Finding 1", "Finding 2"],
            sources=["https://example.com"],
            confidence=0.9,
        )
        d = contract.to_dict()
        self.assertEqual(d["summary"], "Test summary")
        self.assertEqual(len(d["key_findings"]), 2)

        recovered = OutputContract.from_dict(d)
        self.assertEqual(recovered.summary, contract.summary)
        self.assertEqual(recovered.key_findings, contract.key_findings)

    def test_task_serialization(self):
        task = Task(
            task_id="task-123",
            kind=TaskKind.RESEARCH.value,
            instruction="Research topic",
            state=TaskState.RUNNING,
            attempt=2,
            error=StructuredError(code="TEST_ERR", message="Test message"),
        )
        d = task.to_dict()
        recovered = Task.from_dict(d)
        self.assertEqual(recovered.task_id, "task-123")
        self.assertEqual(recovered.state, TaskState.RUNNING)
        self.assertEqual(recovered.attempt, 2)
        self.assertIsNotNone(recovered.error)
        self.assertEqual(recovered.error.code, "TEST_ERR")


class TestConfig(unittest.TestCase):
    def test_config_parsing(self):
        toml_content = """
[daemon]
cooldown_base_s = 2.5
cooldown_cap_s = 60.0
queue_deadline_s = 15.0
max_attempts = 4
default_max_output_chars = 3000

[[workers]]
id = "worker-1"
label = "Worker One"
profile_dir = "/tmp/w1"
enabled = true
concurrency = 1

[[workers]]
id = "worker-2"
label = "Worker Two"
profile_dir = "/tmp/w2"
enabled = false
"""
        cfg = SystemConfig.from_toml_string(toml_content)
        self.assertEqual(cfg.cooldown_base_s, 2.5)
        self.assertEqual(cfg.cooldown_cap_s, 60.0)
        self.assertEqual(cfg.max_attempts, 4)
        self.assertEqual(len(cfg.workers), 2)
        self.assertEqual(cfg.workers[0].label, "Worker One")
        self.assertFalse(cfg.workers[1].enabled)

    def test_config_boundary_violation(self):
        # Boundary: strictly 1-3 workers (accounts 2-4; account 1 is main)
        toml_content = """
[daemon]
[[workers]]
id = "w1"
profile_dir = "/tmp/1"
[[workers]]
id = "w2"
profile_dir = "/tmp/2"
[[workers]]
id = "w3"
profile_dir = "/tmp/3"
[[workers]]
id = "w4"
profile_dir = "/tmp/4"
"""
        with self.assertRaises(ValueError):
            SystemConfig.from_toml_string(toml_content)


class TestValidator(unittest.TestCase):
    def test_valid_json_contract(self):
        raw = json.dumps({
            "summary": "Everything is good",
            "key_findings": ["Key point 1"],
            "sources": [],
            "confidence": 1.0,
            "open_questions": []
        })
        is_valid, contract, err = OutputContractValidator.validate_and_parse(raw, max_chars=1000)
        self.assertTrue(is_valid)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.summary, "Everything is good")

    def test_markdown_fence_extraction(self):
        raw = """Here is the result:
```json
{
  "summary": "Extracted from markdown fence",
  "key_findings": ["Point A"],
  "sources": [],
  "confidence": 0.8,
  "open_questions": []
}
```
Done."""
        is_valid, contract, err = OutputContractValidator.validate_and_parse(raw, max_chars=1000)
        self.assertTrue(is_valid)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.summary, "Extracted from markdown fence")

    def test_character_cap_violation(self):
        raw = json.dumps({
            "summary": "A" * 500,
            "key_findings": ["Long point"],
        })
        is_valid, contract, err = OutputContractValidator.validate_and_parse(raw, max_chars=200)
        self.assertFalse(is_valid)
        self.assertIn("exceeds maximum allowed", err)

    def test_malformed_json(self):
        raw = "This is completely invalid and not JSON at all."
        is_valid, contract, err = OutputContractValidator.validate_and_parse(raw, max_chars=1000)
        self.assertFalse(is_valid)
        self.assertIn("Failed to parse valid JSON", err)


class TestJournal(unittest.TestCase):
    def test_journal_recovery(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = JournalStore(journal_dir=tmp_dir)

            t1 = Task(task_id="t-1", state=TaskState.DONE, result={"res": "ok"})
            t2 = Task(task_id="t-2", state=TaskState.RUNNING, assigned_worker_id="w-1", attempt=1)
            t3 = Task(task_id="t-3", state=TaskState.QUEUED, attempt=1)

            store.save_task(t1)
            store.save_task(t2)
            store.save_task(t3)

            # Create new store instance simulating restart
            new_store = JournalStore(journal_dir=tmp_dir)
            recovered = new_store.recover()

            self.assertEqual(len(recovered), 3)
            self.assertEqual(recovered["t-1"].state, TaskState.DONE)
            # t2 must be recovered to QUEUED with attempt incremented
            self.assertEqual(recovered["t-2"].state, TaskState.QUEUED)
            self.assertEqual(recovered["t-2"].attempt, 2)
            self.assertIsNone(recovered["t-2"].assigned_worker_id)
            self.assertEqual(recovered["t-2"].workers_tried, ["w-1"])
            self.assertEqual(recovered["t-3"].state, TaskState.QUEUED)


class TestWorkers(unittest.TestCase):
    def test_worker_lru_and_cooldown(self):
        cfg = [
            WorkerConfig(id="w1", label="Worker 1", profile_dir="/tmp/w1"),
            WorkerConfig(id="w2", label="Worker 2", profile_dir="/tmp/w2"),
        ]
        reg = WorkerRegistry(cfg)

        # Initially, both are idle. LRU picks first
        chosen1 = reg.get_least_recently_used_idle()
        self.assertIsNotNone(chosen1)
        reg.mark_busy(chosen1.id)

        # Second idle worker picked
        chosen2 = reg.get_least_recently_used_idle()
        self.assertIsNotNone(chosen2)
        self.assertNotEqual(chosen1.id, chosen2.id)
        reg.mark_busy(chosen2.id)

        # No workers available
        self.assertIsNone(reg.get_least_recently_used_idle())

        # Release first worker
        reg.mark_idle(chosen1.id)
        self.assertEqual(reg.get_least_recently_used_idle().id, chosen1.id)

        # Put first worker into cooldown
        duration = reg.mark_cooldown(chosen1.id, base_s=10.0, cap_s=60.0)
        self.assertGreater(duration, 0)
        self.assertEqual(reg.get_worker(chosen1.id).state, WorkerState.COOLDOWN)
        self.assertIsNone(reg.get_least_recently_used_idle())


if __name__ == "__main__":
    unittest.main()
