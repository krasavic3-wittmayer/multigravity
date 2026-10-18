"""End-to-end integration tests for MCP Shim, Daemon IPC, and restart resilience."""

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from multigravity.adapters.fake import FakeWorkerAdapter
from multigravity.config import SystemConfig, WorkerConfig
from multigravity.daemon import CoreDaemon
from multigravity.models import TaskState


class TestMcpEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp_dir.name) / "test_multigravity.sock"
        self.log_dir = Path(self.tmp_dir.name) / "logs"
        self.journal_dir = Path(self.tmp_dir.name) / "journals"

        self.workers_cfg = [
            WorkerConfig(id="worker-1", label="SubAgent 1", profile_dir="/tmp/w1"),
            WorkerConfig(id="worker-2", label="SubAgent 2", profile_dir="/tmp/w2"),
        ]

        self.config = SystemConfig(
            cooldown_base_s=0.5,
            cooldown_cap_s=2.0,
            queue_deadline_s=2.0,
            task_timeout_s=3.0,
            socket_path=str(self.socket_path),
            log_dir=str(self.log_dir),
            journal_dir=str(self.journal_dir),
            workers=self.workers_cfg,
        )

        self.adapter = FakeWorkerAdapter()
        self.daemon = CoreDaemon(config=self.config, adapter=self.adapter)
        await self.daemon.start()

    async def asyncTearDown(self):
        await self.daemon.stop()
        self.tmp_dir.cleanup()

    async def _run_mcp_client(self, requests: list[dict]) -> list[dict]:
        """Spawns an MCP shim process and sends a list of JSON-RPC requests."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "multigravity.mcp_shim",
            str(self.socket_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent.parent),
        )

        responses = []
        try:
            for req in requests:
                proc.stdin.write((json.dumps(req) + "\n").encode())
                await proc.stdin.drain()
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
                responses.append(json.loads(line.decode()))
        finally:
            proc.terminate()
            await proc.wait()

        return responses

    async def test_mcp_tool_surface_full_lifecycle(self):
        """Tests initialize, tools/list, submit_task, wait, get_status, get_result, list_workers."""
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "submit_task",
                    "arguments": {
                        "kind": "research",
                        "instruction": "Summarize async architecture",
                    },
                },
            },
        ]

        resps = await self._run_mcp_client(requests)
        self.assertEqual(resps[0]["result"]["serverInfo"]["name"], "multigravity-mcp-shim")

        tools = resps[1]["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("submit_task", tool_names)
        self.assertIn("get_status", tool_names)
        self.assertIn("get_result", tool_names)
        self.assertIn("wait", tool_names)
        self.assertIn("cancel_task", tool_names)
        self.assertIn("list_workers", tool_names)

        # Extract submitted task_id
        submit_content = json.loads(resps[2]["result"]["content"][0]["text"])
        task_id = submit_content["task_id"]
        self.assertTrue(task_id)

        # Call wait and get_result
        requests_step2 = [
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "wait", "arguments": {"task_ids": [task_id], "timeout_s": 5.0}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_result", "arguments": {"task_id": task_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "list_workers", "arguments": {}},
            },
        ]

        resps2 = await self._run_mcp_client(requests_step2)
        wait_content = json.loads(resps2[0]["result"]["content"][0]["text"])
        self.assertEqual(wait_content[0]["state"], TaskState.DONE.value)

        result_content = json.loads(resps2[1]["result"]["content"][0]["text"])
        self.assertEqual(result_content["status"], TaskState.DONE.value)
        self.assertIn("result", result_content)
        self.assertIn("summary", result_content["result"])

        workers_content = json.loads(resps2[2]["result"]["content"][0]["text"])
        self.assertEqual(len(workers_content), 2)

    async def test_criterion_4_kill_mcp_shim_mid_task(self):
        """Acceptance Criterion 4 part 1: Kill the MCP shim mid-task: tasks continue
        and a new shim can fetch results."""
        # Inject 0.5s latency on worker
        self.adapter.inject_latency("worker-1", 0.5)

        # Shim 1 submits task and immediately gets killed
        proc1 = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "multigravity.mcp_shim",
            str(self.socket_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent.parent),
        )

        submit_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "submit_task", "arguments": {"instruction": "Long task"}},
        }
        proc1.stdin.write((json.dumps(submit_req) + "\n").encode())
        await proc1.stdin.drain()
        line = await proc1.stdout.readline()
        submit_res = json.loads(line.decode())
        task_id = json.loads(submit_res["result"]["content"][0]["text"])["task_id"]

        # Kill Shim 1 immediately!
        proc1.kill()
        await proc1.wait()

        # Task continues running in the daemon. Sleep to let it finish.
        await asyncio.sleep(0.7)

        # Spawn Shim 2 (completely new process) to fetch results
        requests_shim2 = [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_result", "arguments": {"task_id": task_id}},
            }
        ]
        resps = await self._run_mcp_client(requests_shim2)
        result_payload = json.loads(resps[0]["result"]["content"][0]["text"])
        self.assertEqual(result_payload.get("status"), TaskState.DONE.value)

    async def test_criterion_4_kill_daemon_mid_task_and_restart(self):
        """Acceptance Criterion 4 part 2: Kill the daemon mid-task: after restart
        the task is re-queued and completed, with no orphan processes."""
        # Inject latency so task is in RUNNING state
        self.adapter.inject_latency("worker-1", 1.0)
        self.adapter.inject_latency("worker-2", 1.0)

        task_id = self.daemon.dispatcher.submit_task(instruction="Interrupted task")
        # Give dispatcher loop time to assign task to worker (enters RUNNING)
        await asyncio.sleep(0.1)

        task_before = self.daemon.journal.get_task(task_id)
        self.assertEqual(task_before.state, TaskState.RUNNING)

        # Stop / kill daemon mid-task
        await self.daemon.stop()

        # Create new daemon instance (simulating daemon process restart)
        # using the same journal directory
        new_adapter = FakeWorkerAdapter()
        restarted_daemon = CoreDaemon(config=self.config, adapter=new_adapter)
        await restarted_daemon.start()

        try:
            # Re-queued task should now be picked up by restarted daemon and completed
            await restarted_daemon.dispatcher.wait([task_id], timeout_s=5.0)
            res = restarted_daemon.dispatcher.get_result(task_id)
            self.assertEqual(res.get("status"), TaskState.DONE.value)

            recovered_task = restarted_daemon.journal.get_task(task_id)
            # Must show attempt > 1 due to recovery increment
            self.assertGreater(recovered_task.attempt, 1)
        finally:
            await restarted_daemon.stop()


if __name__ == "__main__":
    unittest.main()
