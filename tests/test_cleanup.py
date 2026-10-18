"""Unit tests for multigravity.cleanup module."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from multigravity.cleanup import (
    clean_kill_and_hard_kill,
    cleanup_multigravity,
    find_multigravity_processes,
    is_process_alive,
)
from multigravity.config import SystemConfig, WorkerConfig


class TestCleanup(unittest.TestCase):

    def test_is_process_alive(self):
        # Current process is alive
        self.assertTrue(is_process_alive(os.getpid()))
        # Non-existent high PID is not alive
        self.assertFalse(is_process_alive(99999999))

    def test_clean_kill_responsive_process(self):
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.assertTrue(is_process_alive(proc.pid))

        clean, hard = clean_kill_and_hard_kill([proc.pid], timeout_s=1.5, print_fn=lambda *a: None)
        self.assertIn(proc.pid, clean)
        self.assertEqual(hard, [])
        self.assertFalse(is_process_alive(proc.pid))
        proc.wait()

    def test_hard_kill_persisting_process(self):
        # Spawn process that explicitly ignores SIGTERM
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    "print('READY', flush=True)\n"
                    "while True: time.sleep(1)\n"
                ),
            ],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        # Wait for ready signal
        proc.stdout.readline()
        proc.stdout.close()
        self.assertTrue(is_process_alive(proc.pid))

        clean, hard = clean_kill_and_hard_kill([proc.pid], timeout_s=0.5, print_fn=lambda *a: None)
        self.assertEqual(clean, [])
        self.assertIn(proc.pid, hard)
        self.assertFalse(is_process_alive(proc.pid))
        proc.wait()

    def test_find_multigravity_processes_by_parent(self):
        # Spawn child
        child = subprocess.Popen(["sleep", "30"])
        try:
            matched = find_multigravity_processes(parent_pid=os.getpid())
            self.assertIn(child.pid, matched)
        finally:
            child.kill()
            child.wait()

    from unittest.mock import patch

    @patch("multigravity.cleanup.find_multigravity_processes", return_value=[])
    def test_cleanup_multigravity_file_removal(self, mock_find):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            sock_path = tmp_path / "test.sock"
            sock_path.touch()

            cfg = SystemConfig(socket_path=str(sock_path))
            res = cleanup_multigravity(config=cfg, timeout_s=0.5, print_fn=lambda *a: None)
            self.assertTrue(res["success"])
            self.assertFalse(sock_path.exists())


if __name__ == "__main__":
    unittest.main()

