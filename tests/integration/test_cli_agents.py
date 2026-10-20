import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ab" * 32

RESPONSES: list[str] = []


class _AgentLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length))
        if not RESPONSES:
            return self._reply(500, {"error": {"message": "queue empty"}})
        text = RESPONSES.pop(0)
        self._reply(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            },
        )

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def llm_server() -> Iterator[str]:
    RESPONSES.clear()
    server = HTTPServer(("127.0.0.1", 0), _AgentLLMHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", TEST_KEY)
    return tmp_path


def setup_operation(database: str, llm_url: str, capsys, monkeypatch) -> str:
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Agent CLI",
                "--mode", "ctf-htb", "--include", "example.com",
            ]
        )
        == 0
    )
    operation_id = json.loads(capsys.readouterr().out)["id"]
    plan = {
        "title": "CLI agent plan",
        "tasks": [
            {"id": "probe", "title": "Probe the target", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []}
        ],
        "gates": [],
    }
    plan_path = Path(database).parent / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--db", database, "plan", "create", operation_id, str(plan_path)]) == 0
    capsys.readouterr()
    assert main(["--db", database, "task", "start", operation_id, "probe"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--db", database, "model", "register", "fake",
                "--base-url", f"{llm_url}/v1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    return operation_id


def test_cli_agent_run_list_show(
    home: Path, capsys, llm_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = str(home / "agents.db")
    operation_id = setup_operation(database, llm_server, capsys, monkeypatch)
    RESPONSES.extend(
        [
            '{"steps": ["probe it"]}',
            '{"commands": ['
            '{"type": "request_tool_run", "tool_id": "local.echo", '
            '"params": {"text": "cli-agent-ok"}},'
            '{"type": "note", "key": "shared.done", "value": "yes"},'
            '{"type": "complete_task"}'
            "]}",
        ]
    )

    assert (
        main(
            [
                "--db", database, "agent", "run", operation_id, "probe",
                "--model", "fake/agent@v1",
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
                "--db", database, "agent", "run", operation_id, "probe",
                "--model", "fake/agent@v1", "--approval-id", approval_id,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "completed" in output
    run_id = output.split("\t")[0]

    assert main(["--db", database, "agent", "list", operation_id]) == 0
    listed = capsys.readouterr().out
    assert run_id in listed

    assert main(["--db", database, "--json", "agent", "show", run_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["model_route"] == ["fake/agent@v1"]
    assert report["usage"]["calls"] == 2
    assert report["usage"]["prompt_tokens"] == 80
    assert report["context"]["used_pct"] is not None
    assert report["data_route"][0]["provider"] == "fake"
    assert "shared.done" in report["blackboard_keys"]
    assert report["rejected_commands"] == []
    steps = report["steps"]
    assert any(step.get("step") == "plan" for step in steps)


def test_cli_agent_show_rejected_commands(
    home: Path, capsys, llm_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = str(home / "reject.db")
    operation_id = setup_operation(database, llm_server, capsys, monkeypatch)
    RESPONSES.extend(
        [
            '{"steps": ["s"]}',
            '{"commands": ['
            '{"type": "request_tool_run", "tool_id": "missing.tool", "params": {}},'
            '{"type": "complete_task"}'
            "]}",
        ]
    )
    assert (
        main(
            [
                "--db", database, "agent", "run", operation_id, "probe",
                "--model", "fake/agent@v1",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]
    assert (
        main(["--db", database, "approval", "approve", approval_id, "--actor", "operator"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--db", database, "agent", "run", operation_id, "probe",
                "--model", "fake/agent@v1", "--approval-id", approval_id,
            ]
        )
        == 0
    )
    run_id = capsys.readouterr().out.split("\t")[0]
    assert main(["--db", database, "--json", "agent", "show", run_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["rejected_commands"][0]["command"]["tool_id"] == "missing.tool"
    assert "tool not found" in report["rejected_commands"][0]["reason"]
