"""Unit tests for AgyCliWorkerAdapter isolation, cancellation, and error signatures."""

import asyncio
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from multigravity.adapters.agy_cli import AgyCliWorkerAdapter
from multigravity.models import ErrorCode


class TestAgyCliWorkerAdapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.profile_dir = Path(self.tmp_dir.name) / "test_profile"

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    def test_environment_isolation_and_permissions(self):
        adapter = AgyCliWorkerAdapter()
        env = adapter._setup_worker_env(self.profile_dir)

        self.assertIn("HOME", env)
        self.assertIn("XDG_CONFIG_HOME", env)
        self.assertIn("XDG_DATA_HOME", env)
        self.assertIn("XDG_RUNTIME_DIR", env)

        # Check directories exist with 0700 permissions
        for p in ("home", "config", "data", "cache", "state", "runtime"):
            dir_path = self.profile_dir / p
            self.assertTrue(dir_path.exists())
            mode = dir_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o700)

    async def test_missing_binary_graceful_failure(self):
        adapter = AgyCliWorkerAdapter(agy_binary="/nonexistent/binary/path")
        res = await adapter.run(
            worker_id="w-test",
            prompt="test",
            timeout_s=1.0,
            profile_dir=self.profile_dir,
        )
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ErrorCode.TASK_FAILED.value)

    async def test_cancellation_and_pgid_cleanup(self):
        # Create a mock agy script that ignores flags and sleeps
        dummy_script = Path(self.tmp_dir.name) / "mock_agy.sh"
        dummy_script.write_text("#!/bin/sh\nsleep 10\n")
        dummy_script.chmod(0o755)

        adapter = AgyCliWorkerAdapter(agy_binary=str(dummy_script))

        # Start a run task in background
        run_task = asyncio.create_task(
            adapter.run(
                worker_id="w-hang",
                prompt="some prompt",
                timeout_s=5.0,
                profile_dir=self.profile_dir,
            )
        )

        await asyncio.sleep(0.05)
        # Cancel worker
        await adapter.cancel("w-hang")
        res = await run_task

        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ErrorCode.CANCELLED.value)


if __name__ == "__main__":
    unittest.main()
