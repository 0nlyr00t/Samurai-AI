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
    assert by_id["http.fetch"]["network"] == "direct"
    assert by_id["local.echo"]["network"] == "none"
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


def test_an_allow_rule_approves_without_a_prompt(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "rules.db")
    operation_id = create_operation(database, capsys)
    assert main(["--db", database, "rules", "add", "allow shell.exec: echo *"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "shell.exec",
                "--param",
                "command=echo hi-from-rule",
            ]
        )
        == 0
    )
    assert "hi-from-rule" in capsys.readouterr().out

    assert main(["--db", database, "--json", "operation", "show", operation_id, "--events"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    rules = [e for e in events if e["event_type"] == "permission.rule"]
    assert rules and rules[0]["payload"]["action"] == "allow"
    decided = [
        e for e in events if e["event_type"] == "approval.decided" and "rule:" in str(e["payload"])
    ]
    assert decided


def test_a_deny_rule_stops_before_execution(tmp_path: Path, capsys) -> None:
    database = str(tmp_path / "deny.db")
    operation_id = create_operation(database, capsys)
    assert main(["--db", database, "rules", "add", "deny shell.exec: *rm -rf*"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                database,
                "tool",
                "run",
                operation_id,
                "shell.exec",
                "--param",
                "command=rm -rf /tmp/definitely-missing-dir",
            ]
        )
        == 1
    )
    assert "denied by rule" in capsys.readouterr().err
    assert main(["--db", database, "--json", "operation", "show", operation_id, "--events"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    assert not [e for e in events if e["event_type"].startswith("tool.run")]


NIKTO_MANIFEST = {
    "id": "scan.nikto",
    "title": "Nikto web server scan",
    "image": "sullo/nikto@sha256:" + "0" * 64,
    "executable": "nikto",
    "risk_class": "r2",
    "network": "scoped",
    "proxy_flag": "-useproxy",
    "params": {
        "host": {"type": "string", "required": True, "pattern": "^[a-z0-9.-]+$"},
        "tuning": {
            "type": "array",
            "items_enum": ["1", "2", "3"],
            "max_items": 3,
            "default": ["1", "2"],
        },
    },
    "targets": {"param": "host", "kind": "host", "max": 1},
    "argv": ["-h", "{host}", "-Tuning", "{tuning}", "-nointeractive"],
    "parser": "text-lines",
}


def _isolated_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))


def _install_manifest(tmp_path: Path, capsys, overrides: dict | None = None) -> int:
    database = str(tmp_path / "manifest.db")
    path = tmp_path / "tool.json"
    path.write_text(json.dumps({**NIKTO_MANIFEST, **(overrides or {})}))
    return main(["--db", database, "tool", "install", str(path), "--trusted"])


def test_a_manifest_becomes_a_registered_tool(tmp_path: Path, capsys, monkeypatch) -> None:
    _isolated_home(monkeypatch, tmp_path)
    database = str(tmp_path / "manifest.db")
    path = tmp_path / "tool.json"
    path.write_text(json.dumps(NIKTO_MANIFEST))

    # trust is required before anything lands
    assert main(["--db", database, "tool", "install", str(path)]) == 2
    assert "needs --trusted" in capsys.readouterr().err

    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    assert "installed\tscan.nikto" in capsys.readouterr().out
    # a second install of the same id needs --replace
    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 2
    capsys.readouterr()

    assert main(["--db", database, "tool", "list"]) == 0
    assert "scan.nikto" in capsys.readouterr().out
    assert main(["--db", database, "tool", "manifests"]) == 0
    assert "ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image": "busybox:latest"}, "pinned by digest"),
        ({"parser": "yaml"}, "parser"),
        ({"argv": ["-h", "{nope}"]}, "unknown parameters"),
        (
            {
                "argv": ["-h", "{host}"],
                "params": {
                    "host": {"type": "string", "required": True},
                    "tuning": {"type": "array", "default": ["1"]},
                },
            },
            "used nowhere",
        ),
        ({"network": "none"}, "cannot declare proxy"),
        ({"risk_class": "r0", "read_only": False}, "read-only"),
    ],
)
def test_a_broken_manifest_is_refused(
    tmp_path: Path, capsys, monkeypatch, overrides: dict, message: str
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    assert _install_manifest(tmp_path, capsys, overrides) == 2
    assert message in capsys.readouterr().err


def test_an_edited_manifest_shows_up_as_tampered(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    database = str(tmp_path / "tamper.db")
    path = tmp_path / "tool.json"
    path.write_text(json.dumps(NIKTO_MANIFEST))
    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    capsys.readouterr()

    installed = tmp_path / "home" / "tools" / "scan.nikto.json"
    installed.write_text(json.dumps({**NIKTO_MANIFEST, "title": "Edited after review"}))
    assert main(["--db", database, "tool", "manifests"]) == 0
    assert "tampered" in capsys.readouterr().out


def test_an_installed_manifest_can_be_uninstalled(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    database = str(tmp_path / "uninstall.db")
    path = tmp_path / "tool.json"
    path.write_text(json.dumps(NIKTO_MANIFEST))
    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "tool", "uninstall", "scan.nikto"]) == 0
    assert "removed\tscan.nikto" in capsys.readouterr().out
    assert main(["--db", database, "tool", "uninstall", "scan.nikto"]) == 1
