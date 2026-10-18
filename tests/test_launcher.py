"""Tests for launcher auto-healing and Main Agent fleet prompt generation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from multigravity.config import SystemConfig, WorkerConfig
from multigravity.launcher import (
    auto_heal_stale_processes,
    fix_profile_permissions,
    update_mcp_instructions,
)
from multigravity.prompts import generate_main_agent_prompt


class TestLauncherAndFleetPrompt(unittest.TestCase):
    def test_fleet_prompt_contains_subagent_instructions_and_worker_fleet(self):
        config = SystemConfig(
            workers=[
                WorkerConfig(id="worker-1", label="Account 2", profile_dir="/tmp/w1", enabled=True),
                WorkerConfig(id="worker-2", label="Account 3", profile_dir="/tmp/w2", enabled=True),
                WorkerConfig(id="worker-3", label="Account 4", profile_dir="/tmp/w3", enabled=False),
            ],
            adapter="agy_cli",
        )
        prompt = generate_main_agent_prompt(config)

        # Check worker mentions
        self.assertIn("worker-1", prompt)
        self.assertIn("Account 2", prompt)
        self.assertIn("worker-2", prompt)
        self.assertIn("Account 3", prompt)
        self.assertIn("worker-3", prompt)
        self.assertIn("Disabled", prompt)

        # Check explicit subagent capabilities and instructions
        self.assertIn("invoke_subagent", prompt)
        self.assertIn("Subagent Architecture & Worker Subagents", prompt)
        self.assertIn("propose and explicitly instruct the worker in the task prompt to invoke its own subagents", prompt)
        self.assertIn("burn token quotas on their own separate Google AI Pro accounts", prompt)

        # Check MCP tools
        self.assertIn("submit_task", prompt)
        self.assertIn("wait", prompt)
        self.assertIn("get_result", prompt)
        self.assertIn("list_workers", prompt)

    def test_auto_heal_stale_daemon_pid(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pid_file = tmp_path / "daemon.pid"
            sock_file = tmp_path / "daemon.sock"

            # Use an impossibly high/dead PID (e.g. 99999999)
            dead_pid = 99999999
            pid_file.write_text(str(dead_pid))
            sock_file.touch()

            config = SystemConfig(socket_path=str(sock_file))

            # Monkeypatch get_daemon_pid_path
            from unittest.mock import patch
            with patch("multigravity.launcher.get_daemon_pid_path", return_value=pid_file):
                auto_heal_stale_processes(config)

            self.assertFalse(pid_file.exists(), "Stale PID file should be removed")
            self.assertFalse(sock_file.exists(), "Stale socket file should be removed")

    def test_fix_profile_permissions(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            p_dir = Path(tmp_dir) / "worker-1"
            p_dir.mkdir(parents=True, mode=0o755)
            (p_dir / "home").mkdir(mode=0o755)

            config = SystemConfig(
                workers=[WorkerConfig(id="worker-1", label="Account 2", profile_dir=str(p_dir), enabled=True)]
            )
            fix_profile_permissions(config)

            self.assertEqual(p_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((p_dir / "home").stat().st_mode & 0o777, 0o700)

    def test_launcher_cli_module_output(self):
        repo_root = Path(__file__).parent.parent
        res = subprocess.run(
            [sys.executable, "-m", "multigravity.launcher"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("# Multigravity Fleet & Sub-Agent Orchestration", res.stdout)
        self.assertIn("invoke_subagent", res.stdout)

    def test_ensure_remote_control_active(self):
        from unittest.mock import patch
        from multigravity.launcher import ensure_remote_control_active

        # Mock subprocess.run to simulate inactive status
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "Daemon status: inactive\n"
            ensure_remote_control_active()
            self.assertEqual(mock_run.call_count, 2)
            # Verify the second call starts the daemon
            second_call_args = mock_run.call_args_list[1][0][0]
            self.assertIn("remote-control", second_call_args)
            self.assertIn("start", second_call_args)

    def test_ensure_mcp_permissions_preapproved(self):
        import json
        from multigravity.launcher import ensure_mcp_permissions_preapproved

        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_home = Path(tmp_dir)
            settings_dir = fake_home / ".gemini" / "antigravity-cli"
            settings_dir.mkdir(parents=True)
            settings_file = settings_dir / "settings.json"
            settings_file.write_text(json.dumps({"permissions": {"allow": ["command(ls)"]}}))

            ensure_mcp_permissions_preapproved(settings_file)

            data = json.loads(settings_file.read_text())
            allow = data["permissions"]["allow"]
            self.assertIn("command(ls)", allow)
            self.assertIn("mcp(multigravity/*)", allow)
            self.assertIn("mcp(multigravity/submit_task)", allow)
            self.assertIn("call_mcp_tool(multigravity/*)", allow)



if __name__ == "__main__":
    unittest.main()

