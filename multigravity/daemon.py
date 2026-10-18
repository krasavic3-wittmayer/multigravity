"""Core multigravity daemon orchestrator and lifecycle management."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from multigravity.adapters.base import WorkerAdapter
from multigravity.adapters.fake import FakeWorkerAdapter
from multigravity.config import SystemConfig
from multigravity.dispatcher import Dispatcher
from multigravity.ipc import DaemonIPCServer
from multigravity.journal import JournalStore
from multigravity.logging import StructuredLogger
from multigravity.workers import WorkerRegistry


class CoreDaemon:
    """Long-lived core daemon owning the task queue, dispatcher, and IPC server."""

    def __init__(
        self,
        config: SystemConfig,
        adapter: WorkerAdapter | None = None,
    ):
        self.config = config
        self.journal = JournalStore(config.journal_dir)
        self.logger = StructuredLogger(config.log_dir)
        self.registry = WorkerRegistry(config.workers)
        self.adapter = adapter or FakeWorkerAdapter()

        self.dispatcher = Dispatcher(
            config=self.config,
            registry=self.registry,
            adapter=self.adapter,
            journal=self.journal,
            logger=self.logger,
        )

        self.ipc_server = DaemonIPCServer(
            socket_path=self.config.socket_path,
            dispatch_handler=self._handle_ipc_method,
        )
        self._stop_event = asyncio.Event()

    async def _handle_ipc_method(self, method: str, params: dict[str, Any]) -> Any:
        """Routes IPC tool calls to dispatcher methods."""
        if method == "submit_task":
            task_id = self.dispatcher.submit_task(
                kind=params.get("kind", "research"),
                instruction=params.get("instruction", ""),
                inputs=params.get("inputs"),
                output_spec=params.get("output_spec"),
                max_output_chars=params.get("max_output_chars"),
                mode=params.get("mode", "read_only"),
            )
            return {"task_id": task_id}
        elif method == "get_status":
            task_ids = params.get("task_ids", [])
            return self.dispatcher.get_status(task_ids)
        elif method == "get_result":
            task_id = params.get("task_id", "")
            return self.dispatcher.get_result(task_id)
        elif method == "wait":
            task_ids = params.get("task_ids", [])
            timeout_s = float(params.get("timeout_s", 20.0))
            return await self.dispatcher.wait(task_ids, timeout_s=timeout_s)
        elif method == "cancel_task":
            task_id = params.get("task_id", "")
            cancelled = await self.dispatcher.cancel_task(task_id)
            return {"cancelled": cancelled}
        elif method == "list_workers":
            return self.dispatcher.list_workers()
        else:
            raise ValueError(f"Unknown IPC method '{method}'")

    async def start(self):
        """Starts core daemon subsystems."""
        await self.dispatcher.start()
        await self.ipc_server.start()
        self.logger.log_event("daemon_started", details={"socket": self.config.socket_path})

    async def stop(self):
        """Gracefully stops daemon subsystems and cleans up resources."""
        await self.ipc_server.stop()
        await self.dispatcher.stop()
        self.logger.log_event("daemon_stopped")
        self._stop_event.set()

    async def run_forever(self):
        """Runs the daemon until interrupted by signal."""
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except (NotImplementedError, RuntimeError):
                pass
        await self._stop_event.wait()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = SystemConfig.from_file(config_path) if Path(config_path).exists() else SystemConfig()
    daemon = CoreDaemon(config=cfg)
    try:
        asyncio.run(daemon.run_forever())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
