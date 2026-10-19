import json
import sys
from pathlib import Path

import pytest

from samurai_ai.cli import main

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
            "serverInfo": {"name": "cli-fake-mcp", "version": "1.0"},
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


@pytest.fixture
def server_script(tmp_path: Path) -> str:
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(STDIO_SERVER_SCRIPT, encoding="utf-8")
    return str(script)


def create_operation(database: str, capsys) -> str:
    result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "MCP CLI",
            "--mode",
            "ctf-htb",
            "--include",
            "example.com",
        ]
    )
    assert result == 0
    return json.loads(capsys.readouterr().out)["id"]


def add_stdio_server(database: str, script: str, capsys) -> None:
    assert (
        main(
            [
                "--db",
                database,
                "mcp",
                "add",
                "fake",
                "--transport",
                "stdio",
                "--command",
                json.dumps([sys.executable, script]),
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_cli_mcp_add_list_enable_disable_remove(
    tmp_path: Path, capsys, server_script: str
) -> None:
    database = str(tmp_path / "crud.db")
    add_stdio_server(database, server_script, capsys)

    assert main(["--db", database, "--json", "mcp", "list"]) == 0
    servers = json.loads(capsys.readouterr().out)
    assert servers[0]["name"] == "fake"
    assert servers[0]["transport"] == "stdio"
    assert servers[0]["enabled"] is False
    assert servers[0]["trust_level"] == "untrusted-external"

    assert main(["--db", database, "mcp", "enable", "fake"]) == 0
    assert "enabled" in capsys.readouterr().out

    assert main(["--db", database, "mcp", "disable", "fake"]) == 0
    assert "disabled" in capsys.readouterr().out

    assert main(["--db", database, "mcp", "remove", "fake"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "mcp", "remove", "fake"]) == 2
    assert "MCP server not found" in capsys.readouterr().err


def test_cli_mcp_health_and_tools(tmp_path: Path, capsys, server_script: str) -> None:
    database = str(tmp_path / "health.db")
    add_stdio_server(database, server_script, capsys)

    assert main(["--db", database, "mcp", "health", "fake"]) == 0
    output = capsys.readouterr().out
    assert "protocol=2024-11-05" in output
    assert "ping=ok" in output
    assert "cli-fake-mcp" in output

    assert main(["--db", database, "mcp", "tools", "fake"]) == 0
    output = capsys.readouterr().out
    assert "mcp.fake.echo" in output
    assert "stored 1 tool schema(s)" in output


def test_cli_mcp_invoke_full_approval_flow(
    tmp_path: Path, capsys, server_script: str
) -> None:
    database = str(tmp_path / "invoke.db")
    operation_id = create_operation(database, capsys)
    add_stdio_server(database, server_script, capsys)
    assert main(["--db", database, "mcp", "enable", "fake"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "mcp", "tools", "fake"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "tool", "list"]) == 0
    assert "mcp.fake.echo" in capsys.readouterr().out

    assert (
        main(
            [
                "--db",
                database,
                "mcp",
                "invoke",
                "fake",
                "echo",
                "--operation",
                operation_id,
                "--param",
                "text=hello cli mcp",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "approval required" in error
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]

    assert (
        main(["--db", database, "approval", "approve", approval_id, "--actor", "operator"])
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "mcp",
                "invoke",
                "fake",
                "echo",
                "--operation",
                operation_id,
                "--param",
                "text=hello cli mcp",
                "--approval-id",
                approval_id,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["status"] == "completed"
    assert result["stdout"] == "echo:hello cli mcp"
    assert result["trust"] == "untrusted-external"
    assert result["run"]["tool_id"] == "mcp.fake.echo"
    assert result["run"]["recovery_action_id"]

    run_id = result["run"]["id"]
    assert main(["--db", database, "--json", "tool", "show", run_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["parameters"] == {"text": "hello cli mcp"}


def test_cli_mcp_invoke_unknown_tool_fail_closed(
    tmp_path: Path, capsys, server_script: str
) -> None:
    database = str(tmp_path / "unknown.db")
    operation_id = create_operation(database, capsys)
    add_stdio_server(database, server_script, capsys)
    assert main(["--db", database, "mcp", "enable", "fake"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "mcp", "tools", "fake"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                database,
                "mcp",
                "invoke",
                "fake",
                "missing",
                "--operation",
                operation_id,
            ]
        )
        == 2
    )
    assert "tool not found" in capsys.readouterr().err

    assert (
        main(["--db", database, "--json", "operation", "show", operation_id, "--events"])
        == 0
    )
    events = json.loads(capsys.readouterr().out)["events"]
    assert not [event for event in events if event["event_type"].startswith("tool.run")]


def test_cli_mcp_add_validates_transport(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "invalid.db")
    assert (
        main(["--db", database, "mcp", "add", "bad", "--transport", "stdio"]) == 2
    )
    assert "requires a command" in capsys.readouterr().err
