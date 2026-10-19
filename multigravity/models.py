"""Data models, enums, and structures for multigravity."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkerState(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    COOLDOWN = "COOLDOWN"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    DISABLED = "DISABLED"


class TaskKind(str, Enum):
    RESEARCH = "research"
    DOC_DIGEST = "doc_digest"
    TEST_GEN = "test_gen"
    REVIEW = "review"
    CUSTOM = "custom"


class TaskMode(str, Enum):
    READ_ONLY = "read_only"
    WORKTREE = "worktree"


class ErrorCode(str, Enum):
    NO_WORKER_AVAILABLE = "NO_WORKER_AVAILABLE"
    TASK_FAILED = "TASK_FAILED"
    OUTPUT_CONTRACT_VIOLATION = "OUTPUT_CONTRACT_VIOLATION"
    CANCELLED = "CANCELLED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass
class StructuredError:
    code: str
    message: str
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuredError:
        return cls(
            code=data["code"],
            message=data["message"],
            retryable=data.get("retryable", False),
        )


@dataclass
class OutputContract:
    summary: str
    key_findings: list[str] = field(default_factory=list)
    interface_spec_or_diff: str | None = None
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "summary": self.summary,
            "key_findings": self.key_findings,
            "sources": self.sources,
            "confidence": self.confidence,
            "open_questions": self.open_questions,
        }
        if self.interface_spec_or_diff is not None:
            res["interface_spec_or_diff"] = self.interface_spec_or_diff
        return res

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OutputContract:
        return cls(
            summary=d.get("summary", ""),
            key_findings=list(d.get("key_findings", [])),
            interface_spec_or_diff=d.get("interface_spec_or_diff") or d.get("diff"),
            sources=list(d.get("sources", [])),
            confidence=float(d.get("confidence", 1.0)),
            open_questions=list(d.get("open_questions", [])),
        )


@dataclass
class Task:
    task_id: str
    kind: str = TaskKind.RESEARCH.value
    instruction: str = ""
    inputs: Any = field(default_factory=dict)
    output_spec: dict[str, Any] | None = None
    max_output_chars: int = 4000
    mode: str = TaskMode.READ_ONLY.value
    state: TaskState = TaskState.QUEUED
    attempt: int = 1
    worker_label: str | None = None
    assigned_worker_id: str | None = None
    workers_tried: list[str] = field(default_factory=list)
    created_at: float = 0.0
    enqueued_at: float | None = None
    assigned_at: float | None = None
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: StructuredError | None = None
    raw_transcript_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "instruction": self.instruction,
            "inputs": self.inputs,
            "output_spec": self.output_spec,
            "max_output_chars": self.max_output_chars,
            "mode": self.mode,
            "state": self.state.value if isinstance(self.state, TaskState) else self.state,
            "attempt": self.attempt,
            "worker_label": self.worker_label,
            "assigned_worker_id": self.assigned_worker_id,
            "workers_tried": list(self.workers_tried),
            "created_at": self.created_at,
            "enqueued_at": self.enqueued_at,
            "assigned_at": self.assigned_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error.to_dict() if self.error else None,
            "raw_transcript_path": self.raw_transcript_path,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        err = StructuredError.from_dict(d["error"]) if d.get("error") else None
        state_val = TaskState(d.get("state", TaskState.QUEUED.value))
        return cls(
            task_id=d["task_id"],
            kind=d.get("kind", TaskKind.RESEARCH.value),
            instruction=d.get("instruction", ""),
            inputs=d.get("inputs", {}),
            output_spec=d.get("output_spec"),
            max_output_chars=d.get("max_output_chars", 4000),
            mode=d.get("mode", TaskMode.READ_ONLY.value),
            state=state_val,
            attempt=d.get("attempt", 1),
            worker_label=d.get("worker_label"),
            assigned_worker_id=d.get("assigned_worker_id"),
            workers_tried=list(d.get("workers_tried", [])),
            created_at=d.get("created_at", 0.0),
            enqueued_at=d.get("enqueued_at"),
            assigned_at=d.get("assigned_at"),
            completed_at=d.get("completed_at"),
            result=d.get("result"),
            error=err,
            raw_transcript_path=d.get("raw_transcript_path"),
        )
