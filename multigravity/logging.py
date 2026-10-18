"""Structured JSON-lines logging for multigravity."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from multigravity.isolation import mask_email

EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')


def redact_emails(text: str) -> str:
    """Masks any raw email address in text using the standard masked format (e.g. us***r@example.com)."""
    if not isinstance(text, str):
        return text

    def _replace(match: re.Match) -> str:
        addr = match.group(0)
        start = match.start()
        end = match.end()
        pre = text[max(0, start - 3):start]
        post = text[end:min(len(text), end + 3)]
        if "*" in pre or "*" in post or "*" in addr:
            return addr
        return mask_email(addr)

    return EMAIL_PATTERN.sub(_replace, text)


class StructuredLogger:
    """Appends structured JSON-lines events to a log file with automatic email and credential redaction."""

    def __init__(self, log_dir: str | Path = "logs", filename: str = "events.jsonl"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_file = self.log_dir / filename

    def _sanitize(self, data: Any) -> Any:
        """Strip sensitive token-like keys and redact all email addresses recursively."""
        if isinstance(data, str):
            return redact_emails(data)
        elif isinstance(data, BaseException):
            return redact_emails(f"{type(data).__name__}: {str(data)}")
        elif isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                clean_k = redact_emails(str(k))
                if any(secret in str(k).lower() for secret in ["token", "secret", "key", "password", "auth", "credential"]):
                    sanitized[clean_k] = "[REDACTED]"
                else:
                    sanitized[clean_k] = self._sanitize(v)
            return sanitized
        elif isinstance(data, list):
            return [self._sanitize(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(self._sanitize(item) for item in data)
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
            "event": redact_emails(event_type),
            "task_id": task_id,
            "worker_id": worker_id,
            "details": self._sanitize(details or {}),
        }
        line = json.dumps(event) + "\n"
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)
        return event

    def log_exception(
        self,
        task_id: str | None,
        exc: BaseException,
        message: str = "",
    ) -> dict[str, Any]:
        """Logs an exception with email addresses redacted."""
        return self.log_event(
            "exception",
            task_id=task_id,
            details={
                "message": redact_emails(message),
                "error": redact_emails(f"{type(exc).__name__}: {str(exc)}"),
            },
        )

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
