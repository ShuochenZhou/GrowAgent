"""A lightweight stdio MCP client.

The client discovers tools from MCP server subprocesses and forwards agent tool
calls over newline-delimited JSON-RPC. It intentionally avoids an MCP SDK. Tool
names use the ``mcp__serverName__toolName`` prefix to prevent collisions with
local tools.

Configuration is merged from the user settings file, the project settings file,
and the Claude Code-compatible ``.mcp.json`` file. Example:
{
    "mcpServers": {
        "name": {
            "command": "...",
            "args": [...],
            "env": {...}
        }
    }
}
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import signal
from pathlib import Path
from typing import Any, Awaitable, Callable

from .ui import print_error, print_info

MCP_TOOL_TIMEOUT_SECONDS = 60.0
MCP_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class McpToolError(RuntimeError):
    """A valid MCP response explicitly reported a failed tool operation."""

    def __init__(self, result: dict):
        self.result = result
        content = result.get("content") or []
        text = "\n".join(str(item.get("text", "")) for item in content
                         if isinstance(item, dict) and item.get("type") == "text")
        super().__init__("MCP tool failed: " + (text or json.dumps(result, ensure_ascii=False)))


# One McpConnection owns one MCP server subprocess.


class BorrowedMcpManager:
    """A child may call its inherited tools, but never owns parent processes."""

    def __init__(self, owner: McpManager, allowed_tools: set[str]):
        self._owner = owner
        self._allowed_tools = frozenset(allowed_tools)

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._allowed_tools

    async def call_tool(self, name: str, args: dict) -> str:
        if name not in self._allowed_tools:
            raise ValueError(f"MCP tool not available in this child: {name}")
        return await self._owner.call_tool(name, args)

    async def disconnect_all(self) -> None:
        # The parent's lifetime encloses all child calls and owns cleanup.
        pass


class McpConnection:
    """Manage one MCP server subprocess and its JSON-RPC traffic."""

    def __init__(self, server_name: str, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None):
        # Server name used to prefix and route discovered tools.
        self.server_name = server_name
        # Executable used to start the MCP server.
        self.command = command
        # Arguments passed to the server executable.
        self.args = args or []
        # Additional environment variables merged with the current process environment.
        self.env = env or {}
        # Server subprocess; unset until the connection starts.
        self._process: asyncio.subprocess.Process | None = None
        # Monotonic JSON-RPC request ID used to correlate responses.
        self._next_id = 1
        # Pending requests keyed by JSON-RPC ID.
        self._pending: dict[int, asyncio.Future] = {}
        # Background task that consumes server responses from stdout.
        self._reader_task: asyncio.Task | None = None
        # Background task that drains stderr to prevent backpressure.
        self._stderr_task: asyncio.Task | None = None
        self._reader_error: Exception | None = None

    async def connect(self) -> None:
        """Start the MCP server and its background stream readers."""
        # Merge configured variables into a copy of the current environment.
        merged_env = {**os.environ, **self.env}
        self._reader_error = None
        # Use separate stdio pipes for JSON-RPC requests, responses, and diagnostics.
        self._process = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
            start_new_session=os.name != "nt",
            limit=MCP_MAX_MESSAGE_BYTES,
        )
        # Read output in the background so connect() can continue the handshake.
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        """Consume newline-delimited JSON-RPC responses from server stdout."""
        assert self._process and self._process.stdout
        try:
            while True:
                # MCP stdio uses one JSON-RPC message per line.
                line = await self._process.stdout.readline()
                if not line:
                    # EOF normally means the server process has exited.
                    break
                if len(line) > MCP_MAX_MESSAGE_BYTES:
                    raise ValueError(f"MCP message exceeds {MCP_MAX_MESSAGE_BYTES} bytes")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Ignore malformed output without terminating the connection.
                    continue

                # Responses carry an ID; notifications usually do not.
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if "error" in msg:
                        # Complete the pending future with the server error.
                        e = msg["error"]
                        if not fut.done():
                            fut.set_exception(
                                RuntimeError(f"MCP error {e.get('code')}: {e.get('message')}")
                            )
                    elif not fut.done():
                        # Return only the JSON-RPC result payload to the caller.
                        fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = RuntimeError(f"MCP reader failed (message limit {MCP_MAX_MESSAGE_BYTES} bytes): {exc}")
            self._fail_pending(self._reader_error)
        else:
            self._reader_error = RuntimeError(f"MCP server '{self.server_name}' closed")
            self._fail_pending(self._reader_error)

    async def _drain_stderr(self) -> None:
        """Drain stderr continuously to prevent the subprocess from blocking."""
        assert self._process and self._process.stderr
        try:
            while True:
                chunk = await self._process.stderr.read(65536)
                if not chunk:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _fail_pending(self, error: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()

    async def _send_request(self, method: str, params: dict | None = None) -> Any:
        """Send a JSON-RPC request and await the matching response."""
        if self._reader_error is not None:
            raise RuntimeError(str(self._reader_error))
        assert self._process and self._process.stdin
        # Allocate an ID that the reader loop can map back to this future.
        req_id = self._next_id
        self._next_id += 1

        # JSON-RPC 2.0 request shape:
        # {
        #   "jsonrpc": "2.0",
        #   "id": 1,
        #   "method": "...",
        #   "params": {...}
        # }
        msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        # A newline terminates each stdio message.
        try:
            self._process.stdin.write((msg + "\n").encode())
            await self._process.stdin.drain()
            return await fut
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise
        except Exception:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            raise

    def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification without awaiting a response."""
        if not self._process or not self._process.stdin:
            return
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}})
        self._process.stdin.write((msg + "\n").encode())

    async def initialize(self) -> None:
        """Perform the MCP initialization handshake."""
        # Negotiate protocol and client metadata before any tool requests.
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "growagent", "version": "1.0.0"},
        })
        # Confirm successful initialization with the required notification.
        self._send_notification("notifications/initialized")

    async def list_tools(self) -> list[dict]:
        """Return the tools advertised by this MCP server."""
        result = await self._send_request("tools/list")
        if not result or not isinstance(result.get("tools"), list):
            return []
        # Preserve inputSchema and attach serverName for prefixing and routing.
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema"),
                "serverName": self.server_name,
            }
            for t in result["tools"]
        ]

    async def call_tool(self, name: str, args: dict) -> str:
        """Invoke a server tool and normalize its result to text."""
        result = await self._send_request("tools/call", {"name": name, "arguments": args})
        if isinstance(result, dict) and result.get("isError") is True:
            raise McpToolError(result)
        if isinstance(result, dict) and isinstance(result.get("content"), list):
            # The agent currently consumes text blocks from standard MCP content lists.
            text = "\n".join(
                c["text"] for c in result["content"] if c.get("type") == "text"
            )
            if result.get("structuredContent") is not None:
                text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=False)
            return text or json.dumps(result, ensure_ascii=False)
        # Serialize nonstandard results as JSON so no information is lost.
        return json.dumps(result)

    def close(self) -> None:
        """Close the server process and fail every pending request."""
        if self._reader_task:
            # Stop the background stdout reader.
            self._reader_task.cancel()
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        if self._process:
            try:
                # Kill the subprocess if graceful termination did not complete.
                if getattr(self._process, "stdin", None):
                    self._process.stdin.close()
                if hasattr(self._process, "kill"):
                    if os.name != "nt":
                        os.killpg(self._process.pid, signal.SIGKILL)
                    else:
                        self._process.kill()
            except ProcessLookupError:
                # Ignore races with a process that has already exited.
                pass
            self._process = None
        # Pending requests cannot complete after the connection closes.
        self._fail_pending(RuntimeError(f"MCP server '{self.server_name}' closed"))

    async def aclose(self) -> None:
        process = self._process
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        self.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process:
            await asyncio.wait_for(process.wait(), timeout=5)


# Manage multiple MCP connections and route prefixed tools.


class McpManager:
    """
    Manage all configured MCP server connections.

    Call ``load_and_connect`` to authorize and discover servers, expose the
    returned definitions to the model, and route prefixed tool names through
    ``call_tool``.
    """

    def __init__(self):
        # Active connections keyed by server name.
        self._connections: dict[str, McpConnection] = {}
        # Tool definitions discovered across all active servers.
        self._tools: list[dict] = []
        # Guard against repeated initialization.
        self._connected = False

    async def load_and_connect(
        self, *, authorize_start: Callable[[str, dict], Awaitable[bool]] | None = None
    ) -> None:
        """Authorize each configured process before connecting and discovering tools."""
        if self._connected:
            return
        self._connected = True

        # Merge configuration sources; later sources override duplicate server names.
        configs = self._load_configs()
        if not configs:
            return

        # Bound server initialization so invalid configurations cannot stall startup.
        timeout = 15.0

        for name, cfg in configs.items():
            conn = None
            try:
                # Configuration discovery must never implicitly execute project commands.
                startup = {
                    "server_name": name,
                    "command": shlex.join([cfg["command"], *(cfg.get("args") or [])]),
                    "cwd": str(Path.cwd()),
                }
                if authorize_start is None or not await authorize_start("mcp_start", startup):
                    print_info(f"MCP startup denied: {name}")
                    continue
                conn = McpConnection(name, cfg["command"], cfg.get("args"), cfg.get("env"))
                # Start the process, perform the handshake, and discover tools.
                await conn.connect()
                await asyncio.wait_for(conn.initialize(), timeout=timeout)
                server_tools = await asyncio.wait_for(conn.list_tools(), timeout=timeout)
                # Register the connection only after discovery succeeds.
                self._connections[name] = conn
                self._tools.extend(server_tools)
                print_info(f"MCP connected: {name} ({len(server_tools)} tools)")
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.aclose()
                raise
            except Exception as e:
                # Isolate server failures and clean up the failed subprocess.
                print_error(f"MCP failed to connect: {name}: {e}")
                if conn is not None:
                    await conn.aclose()

    def get_tool_definitions(self) -> list[dict]:
        """Return model-ready tool definitions with MCP name prefixes."""
        return [
            {
                # Prefix tools as mcp__server__tool for collision-free routing.
                "name": f"mcp__{t['serverName']}__{t['name']}",
                "description": t.get("description") or f"MCP tool {t['name']} from {t['serverName']}",
                # Anthropic expects input_schema while MCP exposes inputSchema.
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            }
            for t in self._tools
        ]

    def is_mcp_tool(self, name: str) -> bool:
        """Return whether a tool name uses the MCP prefix."""
        return name.startswith("mcp__")

    async def call_tool(self, prefixed_name: str, args: dict) -> str:
        """Route a prefixed tool call to its MCP server."""
        # Expected format: mcp__serverName__toolName.
        parts = prefixed_name.split("__")
        if len(parts) < 3:
            raise ValueError(f"Invalid MCP tool name: {prefixed_name}")
        server_name = parts[1]
        # Rejoin remaining segments because tool names may contain double underscores.
        tool_name = "__".join(parts[2:])  # tool name might contain __
        conn = self._connections.get(server_name)
        if not conn:
            raise RuntimeError(f"MCP server '{server_name}' not connected")
        try:
            return await asyncio.wait_for(
                conn.call_tool(tool_name, args), timeout=MCP_TOOL_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"MCP tool '{prefixed_name}' timed out after {MCP_TOOL_TIMEOUT_SECONDS:g} seconds"
            ) from exc

    async def disconnect_all(self) -> None:
        """Disconnect every MCP server and clear discovered tools."""
        await asyncio.gather(*(conn.aclose() for conn in self._connections.values()))
        self._connections.clear()
        self._tools.clear()
        self._connected = False

    # Configuration loading

    def _load_configs(self) -> dict[str, dict]:
        """Load and merge MCP server configuration by precedence."""
        merged: dict[str, dict] = {}

        # 1. User configuration.
        global_path = Path.home() / ".growagent" / "settings.json"
        self._merge_config_file(global_path, merged)

        # 2. Project configuration.
        project_path = Path.cwd() / ".growagent" / "settings.json"
        self._merge_config_file(project_path, merged)

        # 3. Claude Code-compatible project configuration.
        mcp_json_path = Path.cwd() / ".mcp.json"
        self._merge_config_file(mcp_json_path, merged)

        return merged

    def _merge_config_file(self, path: Path, target: dict[str, dict]) -> None:
        """Merge one file's mcpServers object into the target mapping."""
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            # Accept either a top-level mcpServers object or the object itself.
            # 1. {"mcpServers": {"name": {...}}}
            # 2. {"name": {...}}
            servers = raw.get("mcpServers", raw)
            for name, config in servers.items():
                # Ignore entries without an executable command.
                if isinstance(config, dict) and "command" in config:
                    target[name] = config
        except Exception:
            # Skip malformed files so one source cannot prevent agent startup.
            pass
