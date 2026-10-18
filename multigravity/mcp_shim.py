"""Thin, stateless stdio MCP shim translating tool calls to the daemon via IPC."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from multigravity.config import SystemConfig
from multigravity.ipc import send_ipc_request
from multigravity.models import TaskKind, TaskMode


MCP_TOOLS_SPEC = [
    {
        "name": "submit_task",
        "description": "Submit a background task to idle worker sub-agents. Returns fast with a task_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [k.value for k in TaskKind],
                    "description": "Task type (research, doc_digest, test_gen, review, custom)",
                    "default": "research",
                },
                "instruction": {
                    "type": "string",
                    "description": "Clear instruction of what the sub-agent should do",
                },
                "inputs": {
                    "type": ["object", "string", "array"],
                    "description": "Context, documents, code snippets, or parameters for the task",
                },
                "output_spec": {
                    "type": "object",
                    "description": "Optional custom output schema overrides",
                },
                "max_output_chars": {
                    "type": "integer",
                    "description": "Character cap for the compressed result (defaults to system setting)",
                },
                "mode": {
                    "type": "string",
                    "enum": [m.value for m in TaskMode],
                    "default": "read_only",
                    "description": "Execution mode: 'read_only' or 'worktree'",
                },
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "get_status",
        "description": "Check current execution status, attempt number, and worker label for one or more tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs to query",
                }
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "get_result",
        "description": "Retrieve the schema-bound, compressed result for a completed task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID whose result to fetch",
                }
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "wait",
        "description": "Wait until any/all specified tasks complete or timeout expires (hard-capped at 25s).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of task IDs to wait on",
                },
                "timeout_s": {
                    "type": "number",
                    "default": 20.0,
                    "description": "Seconds to wait (capped at 25s)",
                },
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel an in-flight or queued task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to cancel",
                }
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_workers",
        "description": "List worker sub-agents and their operational health (IDLE, BUSY, COOLDOWN, AUTH_EXPIRED).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


class McpShim:
    """Stateless MCP stdio server translating tool calls to daemon IPC."""

    def __init__(self, socket_path: str | Path | None = None):
        if socket_path:
            self.socket_path = str(socket_path)
        else:
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
            if xdg_runtime:
                self.socket_path = str(Path(xdg_runtime) / "multigravity.sock")
            else:
                self.socket_path = str(Path("/tmp") / f"multigravity_{os.getuid()}.sock")

    async def run_stdio(self):
        """Main stdio loop reading JSON-RPC requests from stdin."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while not reader.at_eof():
            line = await reader.readline()
            if not line:
                break
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
            except json.JSONDecodeError:
                self._send_error(None, -32700, "Parse error")
                continue

            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            # MCP lifecycle
            if method == "initialize":
                self._send_result(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "multigravity-mcp-shim", "version": "0.1.0"},
                        "capabilities": {"tools": {}},
                    },
                )
            elif method == "notifications/initialized":
                # Notification, no response needed
                pass
            elif method == "tools/list":
                self._send_result(req_id, {"tools": MCP_TOOLS_SPEC})
            elif method == "tools/call":
                tool_name = params.get("name")
                args = params.get("arguments", {})
                await self._handle_tool_call(req_id, tool_name, args)
            else:
                if req_id is not None:
                    self._send_error(req_id, -32601, f"Method '{method}' not found")

    async def _handle_tool_call(self, req_id: Any, tool_name: str, args: dict[str, Any]):
        try:
            # Map tool call directly to daemon method
            ipc_res = await send_ipc_request(
                self.socket_path,
                method=tool_name,
                params=args,
                timeout_s=30.0,
            )
            # Wrap as MCP tool result
            content_text = json.dumps(ipc_res, indent=2) if not isinstance(ipc_res, str) else ipc_res
            self._send_result(req_id, {"content": [{"type": "text", "text": content_text}]})
        except ConnectionRefusedError:
            err_msg = json.dumps({
                "error": {
                    "code": "DAEMON_UNAVAILABLE",
                    "message": "Multigravity core daemon is not currently running or socket is unreachable.",
                    "retryable": True,
                }
            })
            self._send_result(req_id, {"content": [{"type": "text", "text": err_msg}], "isError": True})
        except Exception as e:
            err_msg = json.dumps({
                "error": {
                    "code": "SHIM_ERROR",
                    "message": str(e),
                    "retryable": False,
                }
            })
            self._send_result(req_id, {"content": [{"type": "text", "text": err_msg}], "isError": True})

    def _send_result(self, req_id: Any, result: Any):
        if req_id is None:
            return
        resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    def _send_error(self, req_id: Any, code: int, message: str):
        if req_id is None:
            return
        resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


def main():
    socket_path = sys.argv[1] if len(sys.argv) > 1 else None
    shim = McpShim(socket_path=socket_path)
    asyncio.run(shim.run_stdio())


if __name__ == "__main__":
    main()
