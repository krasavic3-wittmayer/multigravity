"""Configuration parsing and validation using TOML."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkerConfig:
    id: str
    label: str
    profile_dir: str
    enabled: bool = True
    concurrency: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "profile_dir": self.profile_dir,
            "enabled": self.enabled,
            "concurrency": self.concurrency,
        }


@dataclass
class SystemConfig:
    cooldown_base_s: float = 5.0
    cooldown_cap_s: float = 300.0
    queue_deadline_s: float = 300.0
    task_timeout_s: float = 300.0
    max_attempts: int = 3
    default_max_output_chars: int = 4000
    adapter: str = "agy_cli"
    log_dir: str = "logs"
    journal_dir: str = "journals"
    socket_path: str = ""
    workers: list[WorkerConfig] = field(default_factory=list)

    @property
    def enabled_workers(self) -> list[WorkerConfig]:
        return [w for w in self.workers if w.enabled]

    def __post_init__(self):
        if self.adapter not in ("agy_cli", "fake"):
            raise ValueError(
                f"Unknown adapter '{self.adapter}', must be 'agy_cli' or 'fake'"
            )

        if not self.socket_path:
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if xdg_runtime:
                self.socket_path = str(Path(xdg_runtime) / "multigravity.sock")
            else:
                self.socket_path = str(Path("/tmp") / f"multigravity_{os.getuid()}.sock")

        # Boundary check: strictly 1-3 worker accounts (Accounts 2-4; Account 1 is main)
        if len(self.workers) > 3:
            raise ValueError(
                f"Configuration boundary violation: max 3 worker accounts allowed "
                f"(total system limit is 4 accounts including main), found {len(self.workers)}"
            )

    @classmethod
    def from_toml_string(cls, toml_str: str) -> SystemConfig:
        data = tomllib.loads(toml_str)
        daemon_cfg = data.get("daemon", {})
        workers_cfg = data.get("workers", [])

        workers = [
            WorkerConfig(
                id=w["id"],
                label=w.get("label", w["id"]),
                profile_dir=w.get("profile_dir", f"profiles/{w['id']}"),
                enabled=w.get("enabled", True),
                concurrency=w.get("concurrency", 1),
            )
            for w in workers_cfg
        ]

        return cls(
            cooldown_base_s=float(daemon_cfg.get("cooldown_base_s", 5.0)),
            cooldown_cap_s=float(daemon_cfg.get("cooldown_cap_s", 300.0)),
            queue_deadline_s=float(daemon_cfg.get("queue_deadline_s", 300.0)),
            task_timeout_s=float(daemon_cfg.get("task_timeout_s", 300.0)),
            max_attempts=int(daemon_cfg.get("max_attempts", 3)),
            default_max_output_chars=int(daemon_cfg.get("default_max_output_chars", 4000)),
            adapter=str(daemon_cfg.get("adapter", "agy_cli")),
            log_dir=str(daemon_cfg.get("log_dir", "logs")),
            journal_dir=str(daemon_cfg.get("journal_dir", "journals")),
            socket_path=str(daemon_cfg.get("socket_path", "")),
            workers=workers,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> SystemConfig:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with open(path, "rb") as f:
            return cls.from_toml_string(f.read().decode("utf-8"))
