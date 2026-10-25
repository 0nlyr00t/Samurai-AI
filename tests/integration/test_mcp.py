import json
import sqlite3
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    ToolNotFoundError,
    ToolRunStatus,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.mcp import McpServerNotFoundError, McpToolInfo
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.registry import ToolRegistry, default_registry
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.mcp import McpService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import _MIGRATIONS, Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.mcp import McpServerConflictError, McpServerRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository

STDIO_SERVER_SCRIPT = """
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-stdio-mcp", "version": "0.1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text back",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ]
        }
    elif method == "tools/call":
        arguments = request.get("params", {}).get("arguments", {})
        result = {
            "content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}],
            "isError": False,
        }
    else:
        response = {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": -32601, "message": "method not found"},
        }
        sys.stdout.write(json.dumps(response) + "\\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


def _mcp_result(payload: dict[str, Any]) -> dict[str, Any]:
    method = payload.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-http-mcp", "version": "0.2"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "ping-tool",
                    "description": "Ping over HTTP",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    if method == "tools/call":
        return {
            "content": [{"type": "text", "text": "pong"}],
            "isError": False,
        }
    raise ValueError(method)


class _HttpMcpHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length))
        if "id" not in request:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        response = {"jsonrpc": "2.0", "id": request["id"], "result": _mcp_result(request)}
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_mcp_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _HttpMcpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def stdio_server_script(tmp_path: Path) -> str:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(STDIO_SERVER_SCRIPT, encoding="utf-8")
    return str(script)


def make_stack(
    database_path: Path,
) -> tuple[OperationService, KernelService, ToolExecutionService, McpService]:
    database = Database(database_path)
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    mcp = McpService(McpServerRepository(database))
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=default_registry(mcp.resolver()),
    )
    return service, kernel, execution, mcp


def make_operation(service: OperationService, *includes: str) -> Operation:
    return service.create(
        "MCP lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=includes or ("example.com",)),
    )


def test_server_crud_and_enable(tmp_path: Path, stdio_server_script: str) -> None:
    _, _, _, mcp = make_stack(tmp_path / "crud.db")
    server = mcp.add(
        "fake", "stdio", command=(sys.executable, stdio_server_script)
    )
    assert server.enabled is False
    assert server.trust_level == "untrusted-external"
    with pytest.raises(McpServerConflictError):
        mcp.add("fake", "stdio", command=(sys.executable, stdio_server_script))

    server = mcp.set_enabled("fake", True)
    assert server.enabled is True
    assert [entry.name for entry in mcp.list_all()] == ["fake"]

    mcp.remove("fake")
    with pytest.raises(McpServerNotFoundError):
        mcp.get("fake")


def test_stdio_health_and_tools_fetch(tmp_path: Path, stdio_server_script: str) -> None:
    _, _, _, mcp = make_stack(tmp_path / "health.db")
    mcp.add("fake", "stdio", command=(sys.executable, stdio_server_script))

    report = mcp.health("fake")
    assert report["ping"] is True
    assert report["server_info"] == {"name": "fake-stdio-mcp", "version": "0.1"}

    tools = mcp.fetch_tools("fake")
    assert [tool.name for tool in tools] == ["echo"]
    assert mcp.cached_tools("fake") == tools


def test_stdio_mcp_tool_full_governance_flow(
    tmp_path: Path, stdio_server_script: str
) -> None:
    database_path = tmp_path / "run.db"
    service, kernel, execution, mcp = make_stack(database_path)
    mcp.add("fake", "stdio", command=(sys.executable, stdio_server_script))
    mcp.set_enabled("fake", True)
    mcp.fetch_tools("fake")
    operation = make_operation(service)

    tool_id = "mcp.fake.echo"
    definitions = {definition.id for definition in execution.list_tools()}
    assert tool_id in definitions

    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, tool_id, {"text": "hello mcp"})
    assert execution.list_runs(operation.id) == []

    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, tool_id, {"text": "hello mcp"},
        approval_id=caught.value.approval_id,
    )
    assert result.run.status == ToolRunStatus.COMPLETED
    assert result.stdout == "echo:hello mcp"
    assert result.trust.value == "untrusted-external"
    assert result.run.trust.value == "untrusted-external"
    assert result.run.recovery_action_id
    assert result.run.stdout_artifact_id is not None
    artifact = kernel.kernel.get_artifact(result.run.stdout_artifact_id)
    assert artifact is not None
    content = (database_path.parent / artifact.path).read_text(encoding="utf-8")
    assert content == "echo:hello mcp"


def test_mcp_tool_schema_validated_at_run(
    tmp_path: Path, stdio_server_script: str
) -> None:
    service, _, execution, mcp = make_stack(tmp_path / "schema.db")
    mcp.add("fake", "stdio", command=(sys.executable, stdio_server_script))
    mcp.set_enabled("fake", True)
    mcp.fetch_tools("fake")
    operation = make_operation(service)
    with pytest.raises(ToolValidationError, match="missing required"):
        execution.run_tool(operation.id, "mcp.fake.echo", {})
    with pytest.raises(ToolValidationError, match="unknown parameters"):
        execution.run_tool(
            operation.id, "mcp.fake.echo", {"text": "ok", "evil": "param"}
        )
    assert execution.list_runs(operation.id) == []


def test_mcp_resolution_fail_closed(tmp_path: Path, stdio_server_script: str) -> None:
    service, _, execution, mcp = make_stack(tmp_path / "closed.db")
    mcp.add("fake", "stdio", command=(sys.executable, stdio_server_script))
    mcp.set_enabled("fake", True)
    mcp.fetch_tools("fake")
    operation = make_operation(service)

    with pytest.raises(ToolNotFoundError):
        execution.run_tool(operation.id, "mcp.fake.missing", {})
    with pytest.raises(ToolNotFoundError):
        execution.run_tool(operation.id, "mcp.unknown.echo", {})

    mcp.set_enabled("fake", False)
    with pytest.raises(ToolNotFoundError):
        execution.run_tool(operation.id, "mcp.fake.echo", {"text": "x"})
    assert "mcp.fake.echo" not in {
        definition.id for definition in execution.list_tools()
    }


def test_http_mcp_full_flow(tmp_path: Path, http_mcp_server: str) -> None:
    service, kernel, execution, mcp = make_stack(tmp_path / "http.db")
    mcp.add("web", "http", url=http_mcp_server)
    mcp.set_enabled("web", True)
    operation = make_operation(service, "127.0.0.1")

    report = mcp.health("web", operation.scope)
    assert report["server_info"]["name"] == "fake-http-mcp"
    tools = mcp.fetch_tools("web", operation.scope)
    assert [tool.name for tool in tools] == ["ping-tool"]

    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "mcp.web.ping-tool", {})
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "mcp.web.ping-tool", {}, approval_id=caught.value.approval_id
    )
    assert result.run.status == ToolRunStatus.COMPLETED
    assert result.stdout == "pong"
    assert result.trust.value == "untrusted-external"


def test_http_mcp_requires_scope_context(tmp_path: Path, http_mcp_server: str) -> None:
    _, _, _, mcp = make_stack(tmp_path / "noscope.db")
    mcp.add("web", "http", url=http_mcp_server)
    with pytest.raises(Exception, match="requires an operation scope"):
        mcp.health("web")


def test_http_mcp_out_of_scope_endpoint_blocked(tmp_path: Path) -> None:
    service, _, execution, mcp = make_stack(tmp_path / "oob.db")
    mcp.add("web", "http", url="http://10.9.9.9:1/mcp")
    mcp.set_enabled("web", True)
    mcp.repository.set_tools_cache(
        "web",
        [McpToolInfo(name="ping-tool", input_schema={"type": "object", "properties": {}})],
    )
    operation = make_operation(service, "example.com")
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "mcp.web.ping-tool", {})
    execution.kernel.approve(caught.value.approval_id, decided_by="operator")
    with pytest.raises(ToolScopeError):
        execution.run_tool(
            operation.id,
            "mcp.web.ping-tool",
            {},
            approval_id=caught.value.approval_id,
        )


def test_migration_v5_to_v6_adds_mcp_servers(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version, script in _MIGRATIONS[:5]:
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
            )
        connection.commit()
    finally:
        connection.close()

    Database(database_path).initialize()

    connection = sqlite3.connect(database_path)
    try:
        versions = {
            row[0] for row in connection.execute("SELECT version FROM schema_migrations")
        }
        assert versions == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
        connection.execute(
            """
            INSERT INTO mcp_servers(
                id, name, transport, command, url, trust_level, enabled,
                created_at, created_by
            ) VALUES ('mcp_1', 'legacy', 'http', NULL, 'http://127.0.0.1/mcp',
                      'untrusted-external', 0, '2026-01-01T00:00:00', 'test')
            """
        )
        connection.commit()
    finally:
        connection.close()

    server = McpServerRepository(Database(database_path)).get("legacy")
    assert server is not None
    assert server.transport == "http"
    assert server.enabled is False


def test_registry_without_resolver_stays_fail_closed(tmp_path: Path) -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("mcp.any.tool")
