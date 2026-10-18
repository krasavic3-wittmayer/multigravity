"""Real worker adapter driving the official agy CLI under Linux process isolation."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from multigravity.adapters.base import WorkerAdapter, WorkerResult
from multigravity.models import ErrorCode


class AgyCliWorkerAdapter(WorkerAdapter):
    """Drives official agy CLI sub-agents with strict profile and process isolation."""

    def __init__(self, agy_binary: str | None = None, base_worktree_dir: str | Path | None = None):
        self.agy_binary = agy_binary or shutil.which("agy") or "/usr/sbin/agy"
        self.base_worktree_dir = Path(base_worktree_dir or ".worktrees")
        self._running_procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_workers: set[str] = set()

    def _setup_worker_env(self, profile_dir: Path) -> dict[str, str]:
        """Configures isolated Linux profile environment with 0700 permissions."""
        profile_dir = profile_dir.resolve()
        home_dir = profile_dir / "home"
        config_dir = profile_dir / "config"
        data_dir = profile_dir / "data"
        cache_dir = profile_dir / "cache"
        state_dir = profile_dir / "state"
        runtime_dir = profile_dir / "runtime"

        for p in (home_dir, config_dir, data_dir, cache_dir, state_dir, runtime_dir):
            p.mkdir(parents=True, exist_ok=True, mode=0o700)

        env = os.environ.copy()
        env["HOME"] = str(home_dir)
        env["XDG_CONFIG_HOME"] = str(config_dir)
        env["XDG_DATA_HOME"] = str(data_dir)
        env["XDG_CACHE_HOME"] = str(cache_dir)
        env["XDG_STATE_HOME"] = str(state_dir)
        env["XDG_RUNTIME_DIR"] = str(runtime_dir)
        # Avoid leaking parent terminal or session variables
        env.pop("ANTIGRAVITY_CONVERSATION_ID", None)
        return env

    async def run(
        self,
        worker_id: str,
        prompt: str,
        timeout_s: float,
        working_dir: str | None = None,
        profile_dir: str | Path | None = None,
    ) -> WorkerResult:
        if not Path(self.agy_binary).exists():
            return WorkerResult(
                success=False,
                error_code=ErrorCode.TASK_FAILED.value,
                error_message=f"agy binary not found at {self.agy_binary}",
            )

        p_dir = Path(profile_dir or f"profiles/{worker_id}")
        env = self._setup_worker_env(p_dir)
        work_dir = working_dir or str(p_dir / "home")

        cmd = [
            self.agy_binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]

        try:
            # Spawn in a new process group (setsid) so all children can be reaped
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=work_dir,
                start_new_session=True,
            )
            self._running_procs[worker_id] = proc

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            combined = (stdout + " " + stderr).lower()

            # Check for Rate Limit signals
            if "429" in combined or "resource_exhausted" in combined or "quota exceeded" in combined or "rate limit" in combined:
                return WorkerResult(
                    success=False,
                    error_code=ErrorCode.RATE_LIMITED.value,
                    error_message=f"Rate limit signal detected: {stderr.strip()[:200]}",
                )

            # Check for Auth / Session Expiry signals
            auth_sigs = [
                "oauth2: cannot fetch token",
                "keyring error",
                "could not find default credentials",
                "unauthenticated",
                "login required",
                "authentication failed",
            ]
            if any(sig in combined for sig in auth_sigs):
                return WorkerResult(
                    success=False,
                    error_code=ErrorCode.AUTH_EXPIRED.value,
                    error_message=f"Session expiry detected: {stderr.strip()[:200]}",
                )

            if worker_id in self._cancelled_workers or proc.returncode in (-signal.SIGTERM, -signal.SIGKILL):
                return WorkerResult(
                    success=False,
                    error_code=ErrorCode.CANCELLED.value,
                    error_message="Worker process was terminated/cancelled",
                )

            if proc.returncode != 0:
                return WorkerResult(
                    success=False,
                    error_code=ErrorCode.TASK_FAILED.value,
                    error_message=f"CLI exited with status {proc.returncode}: {stderr.strip()[:300]}",
                )

            return WorkerResult(
                success=True,
                raw_output=stdout,
            )

        except asyncio.TimeoutError:
            await self.cancel(worker_id)
            return WorkerResult(
                success=False,
                error_code=ErrorCode.TIMEOUT.value,
                error_message=f"Worker execution timed out after {timeout_s}s",
            )
        except asyncio.CancelledError:
            await self.cancel(worker_id)
            return WorkerResult(
                success=False,
                error_code=ErrorCode.CANCELLED.value,
                error_message="Worker task was cancelled",
            )
        finally:
            self._running_procs.pop(worker_id, None)
            self._cancelled_workers.discard(worker_id)

    async def cancel(self, worker_id: str):
        """Kills the entire process group cleanly to prevent orphan processes."""
        self._cancelled_workers.add(worker_id)
        proc = self._running_procs.get(worker_id)
        if proc and proc.returncode is None:
            try:
                # Terminate the process group
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                # Wait briefly for process to exit
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    os.killpg(pgid, signal.SIGKILL)
                    await proc.wait()
            except (ProcessLookupError, OSError):
                pass

    async def probe(self, worker_id: str, profile_dir: str | Path | None = None) -> bool:
        """Cheap probe running 'agy models' to test connectivity and credentials."""
        p_dir = Path(profile_dir or f"profiles/{worker_id}")
        env = self._setup_worker_env(p_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.agy_binary,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            return proc.returncode == 0
        except Exception:
            return False
