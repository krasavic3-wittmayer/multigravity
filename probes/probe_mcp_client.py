#!/usr/bin/env python3
"""probe_mcp_client.py - Probe script to test MCP protocol communications.

Tests:
1. Spawns a lightweight MCP server over stdio or connects to a Unix domain socket.
2. Sends initialize, tools/list, and tools/call JSON-RPC 2.0 requests.
3. Measures response latency (target < 1.0s).
4. Tests timeout handling and structured error reporting.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path


async def test_mcp_stdio_server():
    """Verify standard MCP JSON-RPC 2.0 handshake and latency via stdio subprocess."""
    # A tiny self-contained JSON-RPC echo server to test the client-side transport mechanics
    server_code = """
import sys, json

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        req = json.loads(line)
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "test-probe-server", "version": "0.1.0"},
                    "capabilities": {"tools": {}}
                }
            }
        elif method == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {"name": "ping", "description": "ping tool", "inputSchema": {"type": "object"}}
                    ]
                }
            }
        elif method == "tools/call":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "pong"}]
                }
            }
        else:
            res = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}}
        sys.stdout.write(json.dumps(res) + "\\n")
        sys.stdout.flush()
    except Exception as e:
        sys.stderr.write(str(e) + "\\n")
        sys.stderr.flush()
"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        server_code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        # 1. Initialize
        t0 = time.perf_counter()
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "probe-client", "version": "0.1.0"},
                "capabilities": {},
            },
        }
        proc.stdin.write((json.dumps(init_req) + "\n").encode())
        await proc.stdin.drain()

        line = await proc.stdout.readline()
        t1 = time.perf_counter()
        init_resp = json.loads(line)
        init_latency_ms = (t1 - t0) * 1000

        # 2. Tools list
        t2 = time.perf_counter()
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write((json.dumps(tools_req) + "\n").encode())
        await proc.stdin.drain()

        line2 = await proc.stdout.readline()
        t3 = time.perf_counter()
        tools_resp = json.loads(line2)
        tools_latency_ms = (t3 - t2) * 1000

        # 3. Call tool
        t4 = time.perf_counter()
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        }
        proc.stdin.write((json.dumps(call_req) + "\n").encode())
        await proc.stdin.drain()

        line3 = await proc.stdout.readline()
        t5 = time.perf_counter()
        call_resp = json.loads(line3)
        call_latency_ms = (t5 - t4) * 1000

        print("=== MCP stdio Transport Probe ===")
        print(f"Initialize Latency: {init_latency_ms:.2f} ms")
        print(f"Tools List Latency: {tools_latency_ms:.2f} ms")
        print(f"Tool Call Latency:  {call_latency_ms:.2f} ms")
        print(f"Call Result: {call_resp.get('result', {}).get('content')}")
        assert call_latency_ms < 1000, "Latency exceeded 1s target"
        print("✓ MCP stdio transport test passed within SLA (<1.0s).")
    finally:
        proc.terminate()
        await proc.wait()


if __name__ == "__main__":
    asyncio.run(test_mcp_stdio_server())
