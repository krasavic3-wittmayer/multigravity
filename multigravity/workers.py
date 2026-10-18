"""Worker registry and state machine management."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from multigravity.config import WorkerConfig
from multigravity.models import WorkerState


@dataclass
class WorkerRecord:
    id: str
    label: str
    profile_dir: str
    state: WorkerState = WorkerState.IDLE
    enabled: bool = True
    concurrency: int = 1
    active_tasks: int = 0
    last_used_at: float = 0.0
    cooldown_until: float = 0.0
    consecutive_429s: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "profile_dir": self.profile_dir,
            "state": self.state.value if isinstance(self.state, WorkerState) else self.state,
            "enabled": self.enabled,
            "concurrency": self.concurrency,
            "active_tasks": self.active_tasks,
            "last_used_at": self.last_used_at,
            "cooldown_until": self.cooldown_until,
            "consecutive_429s": self.consecutive_429s,
        }


class WorkerRegistry:
    """Manages worker state transitions, cooldown timers, and LRU selection."""

    def __init__(self, workers_cfg: list[WorkerConfig] | None = None):
        self._workers: dict[str, WorkerRecord] = {}
        if workers_cfg:
            for cfg in workers_cfg:
                self.register(cfg)

    def register(self, cfg: WorkerConfig):
        initial_state = WorkerState.IDLE if cfg.enabled else WorkerState.DISABLED
        self._workers[cfg.id] = WorkerRecord(
            id=cfg.id,
            label=cfg.label,
            profile_dir=cfg.profile_dir,
            enabled=cfg.enabled,
            concurrency=cfg.concurrency,
            state=initial_state,
        )

    def get_worker(self, worker_id: str) -> WorkerRecord | None:
        return self._workers.get(worker_id)

    def list_workers(self) -> list[dict[str, Any]]:
        """Returns diagnostic worker statuses for main agent list_workers() tool."""
        self._refresh_cooldowns()
        return [
            {
                "id": w.id,
                "label": w.label,
                "state": w.state.value,
                "cooldown_until": w.cooldown_until if w.state == WorkerState.COOLDOWN else None,
            }
            for w in self._workers.values()
        ]

    def _refresh_cooldowns(self, now: float | None = None):
        now = now or time.time()
        for w in self._workers.values():
            if w.state == WorkerState.COOLDOWN and w.cooldown_until <= now:
                w.state = WorkerState.IDLE
                w.cooldown_until = 0.0

    def get_least_recently_used_idle(
        self, excluded_worker_ids: list[str] | None = None
    ) -> WorkerRecord | None:
        """Finds least-recently-used idle worker not in excluded list."""
        self._refresh_cooldowns()
        excluded = set(excluded_worker_ids or [])

        candidates: list[WorkerRecord] = []
        for w in self._workers.values():
            if not w.enabled:
                continue
            if w.id in excluded:
                continue
            if w.state == WorkerState.IDLE and w.active_tasks < w.concurrency:
                candidates.append(w)

        if not candidates:
            return None

        # Sort by last_used_at ascending (LRU)
        candidates.sort(key=lambda w: w.last_used_at)
        return candidates[0]

    def mark_busy(self, worker_id: str):
        w = self._workers.get(worker_id)
        if not w:
            return
        w.active_tasks += 1
        w.last_used_at = time.time()
        w.state = WorkerState.BUSY

    def mark_idle(self, worker_id: str):
        w = self._workers.get(worker_id)
        if not w:
            return
        w.active_tasks = max(0, w.active_tasks - 1)
        if w.active_tasks == 0 and w.state == WorkerState.BUSY:
            w.state = WorkerState.IDLE
            w.consecutive_429s = 0  # Clean run resets 429 counter

    def mark_cooldown(
        self,
        worker_id: str,
        base_s: float = 5.0,
        cap_s: float = 300.0,
        retry_after_s: float | None = None,
    ) -> float:
        w = self._workers.get(worker_id)
        if not w:
            return base_s

        w.active_tasks = max(0, w.active_tasks - 1)
        w.consecutive_429s += 1

        if retry_after_s and retry_after_s > 0:
            duration = retry_after_s
        else:
            # Exponential backoff with jitter
            raw_backoff = min(cap_s, base_s * (2 ** (w.consecutive_429s - 1)))
            jitter = random.uniform(0.8, 1.2)
            duration = min(cap_s, raw_backoff * jitter)

        w.cooldown_until = time.time() + duration
        w.state = WorkerState.COOLDOWN
        return duration

    def mark_auth_expired(self, worker_id: str):
        w = self._workers.get(worker_id)
        if not w:
            return
        w.active_tasks = max(0, w.active_tasks - 1)
        w.state = WorkerState.AUTH_EXPIRED
        w.cooldown_until = 0.0

    def rearm_worker(self, worker_id: str):
        w = self._workers.get(worker_id)
        if not w:
            return
        w.state = WorkerState.IDLE
        w.active_tasks = 0
        w.cooldown_until = 0.0
        w.consecutive_429s = 0

    def any_available_or_cooldown(self) -> bool:
        """Returns True if at least one enabled worker is IDLE, BUSY, or in COOLDOWN."""
        for w in self._workers.values():
            if w.enabled and w.state in (WorkerState.IDLE, WorkerState.BUSY, WorkerState.COOLDOWN):
                return True
        return False
