import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolNotFoundError,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.mcp import (
    MCP_PROTOCOL_VERSION,
    McpProtocolError,
    McpServer,
    McpToolInfo,
    McpValidationError,
    parse_mcp_tool_id,
)
from samurai_ai.execution.mcp import (
    McpClient,
    McpToolAdapter,
    StdioMcpTransport,
    extract_text_content,
    normalize_input_schema,
)
from samurai_ai.execution.registry import ToolRegistry


class FakeTransport:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.sent: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(payload)
        return self.handler(payload)

    async def notify(self, payload: dict[str, Any]) -> None:
        self.notifications.append(payload)

    async def close(self) -> None:
        self.closed = True


def ok_handler(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any]
    if payload["method"] == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "0.1"},
        }
    elif payload["method"] == "ping":
        result = {}
    elif payload["method"] == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        }
    elif payload["method"] == "tools/call":
        text = payload["params"]["arguments"].get("text", "")
        result = {"content": [{"type": "text", "text": f"echo:{text}"}], "isError": False}
    else:
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "error": {"code": -32601, "message": "method not found"},
        }
    return {"jsonrpc": "2.0", "id": payload["id"], "result": result}


def make_server(**overrides: object) -> McpServer:
    payload: dict[str, object] = {
        "name": "fake",
        "transport": "stdio",
        "command": ("python", "server.py"),
        "enabled": True,
    }
    payload.update(overrides)
    return McpServer.model_validate(payload)


def test_server_model_validation() -> None:
    server = make_server()
    assert server.trust_level == "untrusted-external"
    assert server.enabled is True
    with pytest.raises(ValidationError):
        make_server(name="Bad Name")
    with pytest.raises(ValidationError):
        make_server(transport="grpc")
    with pytest.raises(ValidationError):
        make_server(command=None)
    with pytest.raises(ValidationError):
        make_server(transport="http", command=None, url=None)
    with pytest.raises(ValidationError):
        make_server(trust_level="trusted")
    http = make_server(transport="http", command=None, url="http://127.0.0.1/mcp")
    assert http.url == "http://127.0.0.1/mcp"


def test_parse_mcp_tool_id() -> None:
    assert parse_mcp_tool_id("mcp.files.read-file") == ("files", "read-file")
    with pytest.raises(McpValidationError):
        parse_mcp_tool_id("mcp.onlyserver")
    with pytest.raises(McpValidationError):
        parse_mcp_tool_id("mcp.UPPER.tool")
    with pytest.raises(McpValidationError):
        parse_mcp_tool_id("local.echo")


def test_initialize_handshake_and_initialized_notification() -> None:
    transport = FakeTransport(ok_handler)
    client = McpClient(transport)

    async def exercise() -> None:
        await client.initialize()
        assert await client.ping()

    asyncio.run(exercise())
    assert transport.started
    first = transport.sent[0]
    assert first["method"] == "initialize"
    assert first["params"]["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert first["params"]["clientInfo"]["name"] == "samurai-ai"
    assert transport.notifications == [
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ]
    assert client.server_info == {"name": "fake-mcp", "version": "0.1"}


def test_initialize_rejects_wrong_protocol_version() -> None:
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        response = ok_handler(payload)
        if payload["method"] == "initialize":
            response["result"]["protocolVersion"] = "1999-01-01"
        return response

    client = McpClient(FakeTransport(handler))
    with pytest.raises(McpProtocolError, match="protocol version"):
        asyncio.run(client.initialize())


def test_jsonrpc_error_and_id_mismatch_fail_closed() -> None:
    async def unknown_method() -> None:
        client = McpClient(FakeTransport(ok_handler))
        await client.initialize()
        await client._call("unknown/method", {})

    with pytest.raises(McpProtocolError, match="method not found"):
        asyncio.run(unknown_method())

    def mismatched(payload: dict[str, Any]) -> dict[str, Any]:
        response = ok_handler(payload)
        response["id"] = 999
        return response

    bad_client = McpClient(FakeTransport(mismatched))
    with pytest.raises(McpProtocolError, match="id mismatch"):
        asyncio.run(bad_client.initialize())


def test_tools_list_and_call_roundtrip() -> None:
    client = McpClient(FakeTransport(ok_handler))

    async def exercise() -> tuple[list[McpToolInfo], dict[str, Any]]:
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"text": "hi"})
        return tools, result

    tools, result = asyncio.run(exercise())
    assert [tool.name for tool in tools] == ["echo"]
    assert tools[0].input_schema["required"] == ["text"]
    assert extract_text_content(result) == "echo:hi"


def test_normalize_input_schema_fail_closed_defaults() -> None:
    schema = normalize_input_schema({})
    assert schema == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    definition = McpToolAdapter(
        make_server(), McpToolInfo(name="echo", input_schema={})
    ).definition
    with pytest.raises(ToolValidationError, match="unknown parameters"):
        registry.validate_params(definition, {"text": "hi"})


def test_adapter_definition_is_r2_and_schema_validated() -> None:
    tool = McpToolInfo(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    adapter = McpToolAdapter(make_server(transport="http", command=None,
                                         url="http://127.0.0.1/mcp"), tool)
    definition = adapter.definition
    assert definition.id == "mcp.fake.echo"
    assert definition.risk_class == RiskClass.R2
    assert definition.network is NetworkAccess.DIRECT
    assert definition.kind.value == "mcp"

    stdio_adapter = McpToolAdapter(make_server(), tool)
    assert stdio_adapter.definition.network is NetworkAccess.NONE

    registry = ToolRegistry()
    with pytest.raises(ToolValidationError, match="missing required"):
        registry.validate_params(definition, {})
    with pytest.raises(ToolValidationError, match="must be a string"):
        registry.validate_params(definition, {"text": 42})
    assert registry.validate_params(definition, {"text": "ok"}) == {"text": "ok"}


def test_registry_dynamic_resolution_fail_closed() -> None:
    calls: list[str] = []

    class EmptyResolver:
        def resolve(self, tool_id: str) -> None:
            calls.append(tool_id)
            return None

        def definitions(self) -> list[Any]:
            return []

    registry = ToolRegistry(dynamic_resolver=EmptyResolver())
    with pytest.raises(ToolNotFoundError, match="tool not found"):
        registry.get("mcp.missing.tool")
    assert calls == ["mcp.missing.tool"]


def test_extract_text_content_ignores_non_text() -> None:
    result = {
        "content": [
            {"type": "text", "text": "one"},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "two"},
        ]
    }
    assert extract_text_content(result) == "one\ntwo"
    assert extract_text_content({}) == ""


def test_stdio_transport_rejects_unlisted_executable() -> None:
    with pytest.raises(McpValidationError, match="not allowlisted"):
        StdioMcpTransport(("bash", "-c", "id"))
    with pytest.raises(McpValidationError, match="not allowlisted"):
        StdioMcpTransport(("/usr/bin/curl", "http://x"))
