"""Structured JSON-lines logging for multigravity."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class StructuredLogger:
    """Appends structured JSON-lines events to a log file."""

    def __init__(self, log_dir: str | Path = "logs", filename: str = "events.jsonl"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_file = self.log_dir / filename

    def _sanitize(self, data: Any) -> Any:
        """Strip sensitive token-like keys recursively."""
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                if any(secret in k.lower() for secret in ["token", "secret", "key", "password", "auth", "credential"]):
                    sanitized[k] = "[REDACTED]"
                else:
                    sanitized[k] = self._sanitize(v)
            return sanitized
        elif isinstance(data, list):
            return [self._sanitize(item) for item in data]
        return data

    def log_event(
        self,
        event_type: str,
        task_id: str | None = None,
        worker_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event_type,
            "task_id": task_id,
            "worker_id": worker_id,
            "details": self._sanitize(details or {}),
        }
        line = json.dumps(event) + "\n"
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)
        return event

    def task_submitted(self, task_id: str, kind: str, mode: str, max_output_chars: int):
        self.log_event(
            "task_submitted",
            task_id=task_id,
            details={"kind": kind, "mode": mode, "max_output_chars": max_output_chars},
        )

    def task_assigned(
        self,
        task_id: str,
        worker_id: str,
        worker_label: str,
        attempt: int,
        adapter: str | None = None,
    ):
        details: dict[str, Any] = {"worker_label": worker_label, "attempt": attempt}
        if adapter:
            details["adapter"] = adapter
        self.log_event(
            "task_assigned",
            task_id=task_id,
            worker_id=worker_id,
            details=details,
        )

    def task_retried(self, task_id: str, attempt: int, error_code: str, reason: str):
        self.log_event(
            "task_retried",
            task_id=task_id,
            details={"attempt": attempt, "error_code": error_code, "reason": reason},
        )

    def worker_cooldown(self, worker_id: str, duration_s: float, reason: str):
        self.log_event(
            "worker_cooldown",
            worker_id=worker_id,
            details={"duration_s": duration_s, "reason": reason},
        )

    def worker_auth_expired(self, worker_id: str, reason: str):
        self.log_event(
            "worker_auth_expired",
            worker_id=worker_id,
            details={"reason": reason},
        )

    def task_done(self, task_id: str, duration_s: float, adapter: str | None = None):
        details: dict[str, Any] = {"duration_s": duration_s}
        if adapter:
            details["adapter"] = adapter
        self.log_event(
            "task_done",
            task_id=task_id,
            details=details,
        )

    def task_failed(self, task_id: str, error_code: str, message: str):
        self.log_event(
            "task_failed",
            task_id=task_id,
            details={"error_code": error_code, "message": message},
        )

    def task_cancelled(self, task_id: str):
        self.log_event("task_cancelled", task_id=task_id)
