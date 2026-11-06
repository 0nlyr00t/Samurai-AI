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
    '{"type": "note", "key": "shared.done", "value": "yes"},'
    '{"type": "complete_task", "no_finding": "scripted"}'
    "]}"
)


class _HeadlessHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        with LOCK:
            # Executor prompts carry "Current step:" — route them to the
            # executor queue so worker timing cannot mix the roles.
            queue = EXECUTOR_RESPONSES if b"Current step:" in raw else RESPONSES
            if not queue:
                return self._reply(500, {"error": {"message": "queue empty"}})
            text = queue.pop(0)
        self._reply(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
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
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HeadlessHandler)
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
                "--db", database, "--json", "operation", "create", "Headless CI",
                "--mode", "bug-bounty", "--include", "example.com",
            ]
        )
        == 0
    )
    operation_id = json.loads(capsys.readouterr().out)["id"]
    plan = {
        "title": "CI plan",
        "tasks": [
            {"id": "recon", "title": "Recon", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []},
            {"id": "testing", "title": "Testing", "risk_class": "r1",
             "depends_on": [], "required_artifacts": []},
        ],
        "gates": [],
    }
    plan_path = Path(database).parent / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert main(["--db", database, "plan", "create", operation_id, str(plan_path)]) == 0
    capsys.readouterr()
    # a scripted executor runs no tools; the completion gate wants evidence
    for task in ("recon", "testing"):
        assert (
            main(["--db", database, "artifact", "add", operation_id,
                  "--kind", "note", "--text", "seed", "--task", task])
            == 0
        )
        capsys.readouterr()
    assert (
        main(["--db", database, "model", "register", "fake",
              "--base-url", f"{llm_url}/v1"])
        == 0
    )
    capsys.readouterr()
    return operation_id


def test_events_export_ndjson_stdout_and_file(home: Path, capsys, llm_server: str) -> None:
    database = str(home / "events.db")
    operation_id = setup_operation(database, llm_server, capsys)

    assert main(["--db", database, "events", "export", operation_id]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines
    events = [json.loads(line) for line in lines]
    assert all("event_type" in event and "created_at" in event for event in events)
    assert any(event["event_type"] == "operation.created" for event in events)

    dest = home / "journal.ndjson"
    assert (
        main(["--db", database, "events", "export", operation_id, "--out", str(dest)])
        == 0
    )
    capsys.readouterr()
    file_lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(file_lines) == len(lines)

    assert main(["--db", database, "events", "export", "op_missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_headless_run_json_summary_and_exit_codes(
    home: Path, capsys, llm_server: str
) -> None:
    database = str(home / "headless.db")
    operation_id = setup_operation(database, llm_server, capsys)

    # без approval → 2 с инструкцией
    assert (
        main(["--db", database, "headless", "run", operation_id,
              "--model", "fake/agent@v1"])
        == 2
    )
    error = capsys.readouterr().err
    assert "approval required" in error
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]
    assert (
        main(["--db", database, "approval", "approve", approval_id,
              "--actor", "operator"])
        == 0
    )
    capsys.readouterr()

    with LOCK:
        RESPONSES.extend([PLANNER_ANSWER, PLANNER_ANSWER])
        EXECUTOR_RESPONSES.extend([EXECUTOR_ANSWER, EXECUTOR_ANSWER])
    assert (
        main(
            ["--db", database, "headless", "run", operation_id,
             "--model", "fake/agent@v1", "--workers", "2",
             "--approval-id", approval_id]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["workers"] == 2
    assert len(summary["members"]) == 2
    assert summary["usage"]["calls"] == 4
    assert summary["wall_time_seconds"] is not None

    # повторный запуск: готовых задач нет → доменная ошибка → 2
    assert (
        main(["--db", database, "headless", "run", operation_id,
              "--model", "fake/agent@v1", "--approval-id", approval_id])
        == 2
    )
    assert "no ready tasks" in capsys.readouterr().err
