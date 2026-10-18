"""Append-only JSON-lines journal for restart recovery and state persistence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from multigravity.models import StructuredError, Task, TaskState


class JournalStore:
    """Manages task persistence and crash recovery via an append-only JSON-lines journal."""

    def __init__(self, journal_dir: str | Path = "journals", filename: str = "journal.jsonl"):
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal_file = self.journal_dir / filename
        self._tasks: dict[str, Task] = {}
        self._seq = 0

    def recover(self) -> dict[str, Task]:
        """Replays journal from disk and recovers tasks.
        
        Per SPEC section 8: On daemon restart, recover from journal,
        and mark previously RUNNING tasks as QUEUED (attempt + 1).
        """
        self._tasks.clear()
        self._seq = 0

        if not self.journal_file.exists():
            return self._tasks

        with open(self.journal_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                self._seq = max(self._seq, entry.get("seq", 0))
                action = entry.get("action")
                if action == "upsert_task":
                    task_data = entry.get("task")
                    if task_data:
                        task = Task.from_dict(task_data)
                        self._tasks[task.task_id] = task

        # Clean up any tasks that were interrupted in RUNNING state
        running_recovered: list[Task] = []
        for task in self._tasks.values():
            if task.state == TaskState.RUNNING:
                task.state = TaskState.QUEUED
                task.attempt += 1
                if task.assigned_worker_id and task.assigned_worker_id not in task.workers_tried:
                    task.workers_tried.append(task.assigned_worker_id)
                task.assigned_worker_id = None
                task.worker_label = None
                running_recovered.append(task)

        # Write recovery transitions back to journal
        for task in running_recovered:
            self.save_task(task)

        return self._tasks

    def save_task(self, task: Task):
        """Append task state change to journal and update in-memory cache."""
        self._seq += 1
        self._tasks[task.task_id] = task
        entry = {
            "seq": self._seq,
            "timestamp": time.time(),
            "action": "upsert_task",
            "task": task.to_dict(),
        }
        line = json.dumps(entry) + "\n"
        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())
