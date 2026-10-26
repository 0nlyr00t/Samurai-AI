import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ab" * 32

RESPONSES: list[str] = []
EXECUTOR_RESPONSES: list[str] = []
LOCK = threading.Lock()

PLANNER_ANSWER = '{"steps": ["probe it"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "request_tool_run", "tool_id": "local.echo", "params": {"text": "cli-swarm"}},'
    '{"type": "note", "key": "shared.swarm", "value": "ok"},'
    '{"type": "complete_task"}'
    "]}"
)


class _SwarmLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with LOCK:
            # Executor prompts carry "Current step:" — route them to the
            # executor queue so worker timing cannot mix the roles.
            if b"Current step:" in raw:
                if not EXECUTOR_RESPONSES:
                    return self._reply(500, {"error": {"message": "queue empty"}})
                text = EXECUTOR_RESPONSES.pop(0)
            else:
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
    with LOCK:
        RESPONSES.clear()
        EXECUTOR_RESPONSES.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SwarmLLMHandler)
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


def setup_operation(database: str, llm_url: str, capsys) -> str:
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Swarm CLI",
                "--mode", "bug-bounty", "--include", "example.com",
            ]
        )
        == 0
    )
    operation_id = json.loads(capsys.readouterr().out)["id"]
    plan = {
        "title": "CLI swarm plan",
        "tasks": [
            {"id": "recon", "title": "Recon the target", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []},
            {"id": "testing", "title": "Test the target", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []},
            {"id": "report", "title": "Report", "risk_class": "r0",
             "depends_on": ["recon", "testing"], "required_artifacts": []},
        ],
        "gates": [],
    }
    plan_path = Path(database).parent / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--db", database, "plan", "create", operation_id, str(plan_path)]) == 0
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


def test_cli_swarm_run_ready_list_show(
    home: Path, capsys, llm_server: str
) -> None:
    database = str(home / "swarm.db")
    operation_id = setup_operation(database, llm_server, capsys)

    assert main(["--db", database, "swarm", "ready", operation_id]) == 0
    ready = capsys.readouterr().out
    assert "recon" in ready and "testing" in ready and "report" not in ready

    assert (
        main(["--db", database, "swarm", "run", operation_id, "--model", "fake/agent@v1"])
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

    with LOCK:
        RESPONSES.extend([PLANNER_ANSWER, PLANNER_ANSWER])
        EXECUTOR_RESPONSES.extend([EXECUTOR_ANSWER, EXECUTOR_ANSWER])
    assert (
        main(
            [
                "--db", database, "swarm", "run", operation_id,
                "--model", "fake/agent@v1",
                "--workers", "2",
                "--spec", "recon:recon",
                "--spec", "testing:testing",
                "--approval-id", approval_id,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "completed" in output
    swarm_id = output.split("\t")[0]
    assert swarm_id.startswith("swarm_")
    assert "recon" in output and "testing" in output

    assert main(["--db", database, "swarm", "list", operation_id]) == 0
    assert swarm_id in capsys.readouterr().out

    assert main(["--db", database, "--json", "swarm", "show", swarm_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"
    assert report["workers"] == 2
    assert len(report["members"]) == 2
    specializations = {m["task_id"]: m["specialization"] for m in report["members"]}
    assert specializations == {"recon": "recon", "testing": "testing"}
    assert report["usage"]["calls"] == 4
    assert report["usage"]["prompt_tokens"] == 160
    assert report["wall_time_seconds"] is not None
    assert report["speedup"] is not None

    # после завершения recon/testing готова зависимая задача report
    assert main(["--db", database, "swarm", "ready", operation_id]) == 0
    assert "report" in capsys.readouterr().out


def test_cli_swarm_explicit_tasks_and_errors(
    home: Path, capsys, llm_server: str
) -> None:
    database = str(home / "swarm-errors.db")
    operation_id = setup_operation(database, llm_server, capsys)

    assert (
        main(
            [
                "--db", database, "swarm", "run", operation_id,
                "--model", "fake/agent@v1", "--task", "missing",
                "--approval-id", "appr_none",
            ]
        )
        == 2
    )
    assert "unknown task" in capsys.readouterr().err

    assert (
        main(
            [
                "--db", database, "swarm", "run", operation_id,
                "--model", "fake/agent@v1", "--task", "recon",
                "--spec", "recon:bogus", "--approval-id", "appr_none",
            ]
        )
        == 2
    )
    assert "unknown specialization" in capsys.readouterr().err

    assert (
        main(
            [
                "--db", database, "swarm", "run", operation_id,
                "--model", "fake/agent@v1", "--task", "report",
                "--approval-id", "appr_none",
            ]
        )
        == 2
    )
    assert "unfinished dependencies" in capsys.readouterr().err

    assert main(["--db", database, "swarm", "show", "swarm_missing"]) == 2
    assert "swarm not found" in capsys.readouterr().err


def test_cli_swarm_run_with_explicit_task_subset(
    home: Path, capsys, llm_server: str
) -> None:
    database = str(home / "swarm-subset.db")
    operation_id = setup_operation(database, llm_server, capsys)
    assert (
        main(["--db", database, "swarm", "run", operation_id, "--model", "fake/agent@v1"])
        == 2
    )
    error = capsys.readouterr().err
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]
    assert (
        main(["--db", database, "approval", "approve", approval_id, "--actor", "operator"])
        == 0
    )
    capsys.readouterr()

    with LOCK:
        RESPONSES.extend([PLANNER_ANSWER])
        EXECUTOR_RESPONSES.extend([EXECUTOR_ANSWER])
    assert (
        main(
            [
                "--db", database, "swarm", "run", operation_id,
                "--model", "fake/agent@v1", "--task", "recon",
                "--approval-id", approval_id,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "completed" in output
    assert "testing" not in output
