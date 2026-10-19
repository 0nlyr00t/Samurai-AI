"""Minimal MCP client (JSON-RPC 2.0, protocol revision 2024-11-05).

Implements the initialize handshake, notifications/initialized, ping,
tools/list and tools/call over two transports:

- stdio: a long-running server process spawned without a shell
  (``asyncio.create_subprocess_exec``), newline-delimited JSON-RPC frames,
  per-request timeouts and process-group teardown on close;
- http: Streamable-HTTP-style POST of JSON-RPC frames through the governed
  HttpEgressBroker, so operation scope, DNS and redirect revalidation apply
  to MCP endpoints exactly as they do to http.fetch.

There is no external MCP SDK dependency by design.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Protocol

from samurai_ai.domain.execution import ToolDefinition, ToolKind
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.mcp import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_PROTOCOL_VERSION,
    McpProtocolError,
    McpServer,
    McpToolInfo,
    McpTransportError,
    McpValidationError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.http import HttpEgressBroker
from samurai_ai.execution.registry import ToolOutput

MAX_FRAME_BYTES = 1 << 20
DEFAULT_REQUEST_TIMEOUT = 15.0
STDIO_ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {Path(sys.executable).name, "python", "python3", "uvx", "uv", "npx", "node"}
)


class McpTransport(Protocol):
    async def start(self) -> None: ...

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def notify(self, payload: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class StdioMcpTransport:
    def __init__(
        self,
        command: tuple[str, ...],
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        if not command:
            raise McpValidationError("stdio MCP transport requires a command")
        executable = Path(command[0]).name
        if executable not in STDIO_ALLOWED_EXECUTABLES:
            raise McpValidationError(
                f"MCP stdio executable {command[0]!r} is not allowlisted "
                f"({', '.join(sorted(STDIO_ALLOWED_EXECUTABLES))})"
            )
        if any("\x00" in part for part in command):
            raise McpValidationError("MCP stdio command cannot contain NUL bytes")
        self.command = command
        self.request_timeout = request_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_chunks: list[bytes] = []
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_FRAME_BYTES,
            )
        except OSError as exc:
            raise McpTransportError(
                f"failed to spawn MCP server {self.command[0]!r}: {exc}"
            ) from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._require_process()
        frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(frame) > MAX_FRAME_BYTES:
            raise McpProtocolError("MCP request frame exceeds the 1 MiB limit")
        async with self._write_lock:
            assert process.stdin is not None
            process.stdin.write(frame)
            await process.stdin.drain()
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(), timeout=self.request_timeout
            )
        except TimeoutError as exc:
            raise McpTransportError(
                f"MCP server {self.command[0]!r} did not respond within "
                f"{self.request_timeout}s"
            ) from exc
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise McpProtocolError("MCP response frame exceeds the 1 MiB limit") from exc
        if not line:
            stderr = b"".join(self._stderr_chunks).decode("utf-8", errors="replace")
            raise McpTransportError(
                f"MCP server {self.command[0]!r} closed stdout "
                f"(exit={process.returncode}, stderr={stderr[:512].strip()!r})"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(f"invalid JSON-RPC frame from MCP server: {exc}") from exc
        if not isinstance(response, dict):
            raise McpProtocolError("MCP response frame must be a JSON object")
        return response

    async def notify(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            assert process.stdin is not None
            process.stdin.write(frame)
            await process.stdin.drain()

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if process.returncode is None:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    if sys.platform != "win32":
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    else:
                        process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
                await process.wait()

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise McpTransportError("MCP stdio transport is not started")
        return self._process

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        captured = 0
        while True:
            chunk = await process.stderr.read(65536)
            if not chunk:
                return
            remaining = 65536 - captured
            if remaining > 0:
                self._stderr_chunks.append(chunk[:remaining])
                captured += len(chunk[:remaining])


class HttpMcpTransport:
    def __init__(
        self,
        url: str,
        broker: HttpEgressBroker,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.url = url
        self.broker = broker
        self.request_timeout = request_timeout

    async def start(self) -> None:
        return None

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = await asyncio.to_thread(
            self.broker.fetch,
            self.url,
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self.request_timeout,
            max_bytes=MAX_FRAME_BYTES,
        )
        if response.status >= 400:
            raise McpTransportError(
                f"MCP endpoint {self.url} returned HTTP {response.status}"
            )
        try:
            decoded = json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise McpProtocolError(f"invalid JSON-RPC response from {self.url}") from exc
        if not isinstance(decoded, dict):
            raise McpProtocolError("MCP response must be a JSON object")
        return decoded

    async def notify(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        await asyncio.to_thread(
            self.broker.fetch,
            self.url,
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self.request_timeout,
            max_bytes=MAX_FRAME_BYTES,
        )

    async def close(self) -> None:
        return None


class McpClient:
    def __init__(self, transport: McpTransport) -> None:
        self.transport = transport
        self._next_id = 0
        self.server_info: dict[str, Any] = {}

    async def initialize(self) -> dict[str, Any]:
        await self.transport.start()
        result = await self._call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": MCP_CLIENT_NAME, "version": MCP_CLIENT_VERSION},
            },
        )
        if not isinstance(result, dict):
            raise McpProtocolError("initialize result must be an object")
        protocol = result.get("protocolVersion")
        if protocol != MCP_PROTOCOL_VERSION:
            raise McpProtocolError(
                f"unsupported MCP protocol version {protocol!r} "
                f"(expected {MCP_PROTOCOL_VERSION!r})"
            )
        server_info = result.get("serverInfo")
        if isinstance(server_info, dict):
            self.server_info = server_info
        await self.transport.notify(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        return result

    async def ping(self) -> bool:
        await self._call("ping", None)
        return True

    async def list_tools(self) -> list[McpToolInfo]:
        result = await self._call("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpProtocolError("tools/list result must contain a tools array")
        parsed: list[McpToolInfo] = []
        for entry in tools:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise McpProtocolError("tools/list entries must be objects with a name")
            schema = entry.get("inputSchema")
            parsed.append(
                McpToolInfo(
                    name=entry["name"],
                    description=str(entry.get("description") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                )
            )
        return parsed

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if not isinstance(result, dict):
            raise McpProtocolError("tools/call result must be an object")
        return result

    async def close(self) -> None:
        await self.transport.close()

    async def _call(self, method: str, params: dict[str, Any] | None) -> Any:
        self._next_id += 1
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        response = await self.transport.request(request)
        if response.get("jsonrpc") != "2.0" or response.get("id") != self._next_id:
            raise McpProtocolError(
                f"MCP response id mismatch for {method}: {response.get('id')!r}"
            )
        error = response.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise McpProtocolError(f"MCP error {code} from {method}: {message}")
        return response.get("result")


def build_transport(server: McpServer, scope: ScopeContract | None) -> McpTransport:
    if server.transport == "stdio":
        assert server.command is not None
        return StdioMcpTransport(server.command)
    if scope is None:
        raise McpValidationError(
            f"http MCP server {server.name!r} requires an operation scope context"
        )
    assert server.url is not None
    return HttpMcpTransport(server.url, HttpEgressBroker(scope))


def normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(schema) if schema else {}
    normalized["type"] = "object"
    properties = normalized.get("properties")
    normalized["properties"] = properties if isinstance(properties, dict) else {}
    required = normalized.get("required")
    normalized["required"] = (
        [str(name) for name in required] if isinstance(required, list) else []
    )
    normalized.setdefault("additionalProperties", False)
    return normalized


def extract_text_content(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for entry in content:
        if isinstance(entry, dict) and entry.get("type") == "text":
            texts.append(str(entry.get("text", "")))
    return "\n".join(texts)


class McpToolAdapter:
    def __init__(self, server: McpServer, tool: McpToolInfo) -> None:
        self.server = server
        self.tool = tool
        self._definition = ToolDefinition(
            id=f"mcp.{server.name}.{tool.name}",
            version=1,
            kind=ToolKind.MCP,
            description=tool.description or f"MCP tool {tool.name} on {server.name}",
            risk_class=RiskClass.R2,
            network_access=server.transport == "http",
            timeout_seconds=60,
            output_limit_bytes=65536,
            parameters=normalize_input_schema(tool.input_schema),
            capabilities=("mcp", server.name),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        arguments = {key: value for key, value in params.items() if key != "_run_id"}
        return asyncio.run(self._invoke(arguments, scope))

    async def _invoke(self, arguments: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        client = McpClient(build_transport(self.server, scope))
        try:
            await client.initialize()
            result = await client.call_tool(self.tool.name, arguments)
        finally:
            await client.close()
        stdout = extract_text_content(result)
        server_name = self.server_info_name(client)
        stderr = f"mcp {self.server.name}/{self.tool.name} via {self.server.transport}"
        if server_name:
            stderr += f" ({server_name})"
        if result.get("isError"):
            stderr += " [tool reported error]"
            return ToolOutput(stdout=stdout, stderr=stderr, exit_code=1)
        return ToolOutput(stdout=stdout, stderr=stderr, exit_code=0)

    @staticmethod
    def server_info_name(client: McpClient) -> str:
        name = client.server_info.get("name")
        version = client.server_info.get("version")
        if isinstance(name, str) and isinstance(version, str):
            return f"{name} {version}"
        return name if isinstance(name, str) else ""
