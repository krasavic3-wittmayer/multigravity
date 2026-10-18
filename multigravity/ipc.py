"""Unix Domain Socket IPC protocol between MCP shim and Core Daemon."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Coroutine


class DaemonIPCServer:
    """Unix Domain Socket server owned by the core daemon."""

    def __init__(
        self,
        socket_path: str | Path,
        dispatch_handler: Callable[[str, dict[str, Any]], Coroutine[Any, Any, Any]],
    ):
        self.socket_path = Path(socket_path)
        self.dispatch_handler = dispatch_handler
        self.server: asyncio.Server | None = None

    async def start(self):
        # Remove stale socket if exists
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        # Ensure parent dir exists with 0700
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )

        # Set permissions to 0600 (owner only)
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        try:
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
                    err_resp = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                    writer.write((json.dumps(err_resp) + "\n").encode())
                    await writer.drain()
                    continue

                req_id = req.get("id")
                method = req.get("method", "")
                params = req.get("params", {})

                try:
                    res = await self.dispatch_handler(method, params)
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": res}
                except Exception as e:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": str(e)},
                    }

                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def send_ipc_request(
    socket_path: str | Path, method: str, params: dict[str, Any], timeout_s: float = 30.0
) -> Any:
    """Utility for MCP shim to send a request over the Unix domain socket."""
    socket_path = str(socket_path)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        if not line:
            raise ConnectionError("Daemon closed connection without response")

        resp = json.loads(line.decode("utf-8"))
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"IPC Error [{err.get('code')}]: {err.get('message')}")
        return resp.get("result")
    finally:
        writer.close()
        await writer.wait_closed()
