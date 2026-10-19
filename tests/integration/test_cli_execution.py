import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.cli import main


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"cli-http-body"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def create_operation(database: str, capsys) -> str:
    result = main(
        [
            "--db",
            database,
            "--json",
            "operation",
            "create",
            "Tool CLI",
            "--mode",
            "bug-bounty",
            "--include",
            "127.0.0.1",
        ]
    )
    assert result == 0
    return json.loads(capsys.readouterr().out)["id"]


def test_cli_tool_list(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "tools.db")
    assert main(["--db", database, "tool", "list"]) == 0
    output = capsys.readouterr().out
    assert "local.echo" in output
    assert "process.exec" in output
    assert "http.fetch" in output
    assert "r2" in output

    assert main(["--db", database, "--json", "tool", "list"]) == 0
    tools = json.loads(capsys.readouterr().out)
    by_id = {tool["id"]: tool for tool in tools}
    assert by_id["http.fetch"]["network_access"] is True
    assert by_id["local.echo"]["risk_class"] == "r0"


def test_cli_tool_run_and_show_echo(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "run.db")
    operation_id = create_operation(database, capsys)

    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "tool",
                "run",
                operation_id,
                "local.echo",
                "--param",
                "text=hello cli",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["status"] == "completed"
    assert result["run"]["exit_code"] == 0
    assert result["stdout"] == "hello cli"
    assert result["trust"] == "untrusted-external"
    run_id = result["run"]["id"]

    assert main(["--db", database, "--json", "tool", "show", run_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == run_id
    assert shown["tool_id"] == "local.echo"
    assert shown["recovery_action_id"]

    assert main(["--db", database, "tool", "show", run_id]) == 0
    human = capsys.readouterr().out
    assert "completed" in human
    assert run_id in human


def test_cli_tool_run_process_exec(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "process.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "process.exec",
                "--param",
                'argv=["echo", "via-cli"]',
            ]
        )
        == 0
    )
    assert "via-cli" in capsys.readouterr().out


def test_cli_tool_run_unknown_tool_fails(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "unknown.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(["--db", database, "tool", "run", operation_id, "missing.tool"]) == 2
    )
    assert "tool not found" in capsys.readouterr().err


def test_cli_http_fetch_approval_flow(tmp_path: Path, capsys, http_server: str) -> None:
    database = str(tmp_path / "approval.db")
    operation_id = create_operation(database, capsys)

    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "tool",
                "run",
                operation_id,
                "http.fetch",
                "--param",
                f"url={http_server}/",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"] == "approval-required"
    approval_id = error["approval_id"]

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
                "tool",
                "run",
                operation_id,
                "http.fetch",
                "--param",
                f"url={http_server}/",
                "--approval-id",
                approval_id,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["run"]["status"] == "completed"
    assert result["stdout"] == "cli-http-body"
    assert result["run"]["approval_id"] == approval_id


def test_cli_http_fetch_human_approval_error(tmp_path: Path, capsys, http_server: str) -> None:
    database = str(tmp_path / "human.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "http.fetch",
                "--param",
                f"url={http_server}/",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "approval required" in captured.err
    assert "apr_" in captured.err
