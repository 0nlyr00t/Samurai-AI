import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ab" * 32

RESPONSES: list[str] = []
PROMPTS: list[str] = []
LOCK = threading.Lock()

PLANNER_ANSWER = '{"steps": ["probe it"]}'
EXECUTOR_ANSWER = (
    '{"commands": ['
    '{"type": "note", "key": "shared.done", "value": "yes"},'
    '{"type": "complete_task", "no_finding": "scripted"}'
    "]}"
)


class _CapturingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length))
        with LOCK:
            for message in payload.get("messages", []):
                PROMPTS.append(str(message.get("content", "")))
            if not RESPONSES:
                return self._reply(500, {"error": {"message": "queue empty"}})
            text = RESPONSES.pop(0)
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
        PROMPTS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CapturingHandler)
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


def test_cli_memory_crud(home: Path, capsys) -> None:
    database = str(home / "memory.db")
    assert (
        main(
            [
                "--db", database, "--json", "memory", "add", "acme-bb",
                "Baseline recon", "--body", "enumerate subdomains first",
                "--kind", "methodology", "--tags", "recon,osint",
                "--retention-days", "30",
            ]
        )
        == 0
    )
    entry = json.loads(capsys.readouterr().out)
    entry_id = entry["id"]
    assert entry["workspace"] == "acme-bb"
    assert entry["kind"] == "methodology"
    assert entry["retention_days"] == 30

    assert main(["--db", database, "memory", "list", "--workspace", "acme-bb"]) == 0
    assert "Baseline recon" in capsys.readouterr().out
    assert main(["--db", database, "memory", "workspaces"]) == 0
    assert "acme-bb" in capsys.readouterr().out

    assert (
        main(["--db", database, "memory", "update", entry_id, "--body", "updated body"])
        == 0
    )
    assert "v2" in capsys.readouterr().out
    assert main(["--db", database, "--json", "memory", "show", entry_id]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["body"] == "updated body"
    assert shown["version"] == 2

    assert main(["--db", database, "memory", "delete", entry_id]) == 0
    capsys.readouterr()
    assert main(["--db", database, "memory", "show", entry_id]) == 2
    assert "not found" in capsys.readouterr().err

    assert main(["--db", database, "--json", "memory", "purge"]) == 0
    assert json.loads(capsys.readouterr().out)["purged"] == []


def test_cli_memory_validation_errors(home: Path, capsys) -> None:
    database = str(home / "memory-errors.db")
    assert (
        main(["--db", database, "memory", "add", "Bad Workspace", "title"]) == 2
    )
    assert "kebab-case" in capsys.readouterr().err
    assert main(["--db", database, "memory", "delete", "mem_missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_skill_install_requires_trust(home: Path, capsys) -> None:
    database = str(home / "skills.db")
    skill_file = home / "web-recon.md"
    skill_file.write_text("Prefer passive enumeration first.", encoding="utf-8")

    assert (
        main(["--db", database, "skill", "install", "web-recon", str(skill_file)]) == 2
    )
    error = capsys.readouterr().err
    assert "trust" in error and "sha256" in error

    assert (
        main(
            ["--db", database, "skill", "install", "web-recon", str(skill_file),
             "--trusted"]
        )
        == 0
    )
    assert "v1" in capsys.readouterr().out

    skill_file.write_text("Passive first, then active fingerprinting.", encoding="utf-8")
    assert (
        main(
            ["--db", database, "skill", "install", "web-recon", str(skill_file),
             "--trusted"]
        )
        == 0
    )
    assert "v2" in capsys.readouterr().out

    assert main(["--db", database, "skill", "list"]) == 0
    assert "web-recon\tv2" in capsys.readouterr().out
    assert main(["--db", database, "skill", "show", "web-recon"]) == 0
    assert "fingerprinting" in capsys.readouterr().out
    assert main(["--db", database, "skill", "versions", "web-recon"]) == 0
    versions = capsys.readouterr().out
    assert "v1" in versions and "v2" in versions
    assert main(["--db", database, "skill", "show", "missing"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_custom_playbook_install_and_use(home: Path, capsys) -> None:
    database = str(home / "playbooks.db")
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Custom PB",
                "--mode", "bug-bounty", "--include", "example.com",
            ]
        )
        == 0
    )
    operation_id = json.loads(capsys.readouterr().out)["id"]

    template = {
        "name": "quick-recon",
        "title": "Quick recon pass",
        "tasks": [
            {"id": "scope-review", "title": "Review scope", "risk_class": "r0",
             "depends_on": [], "required_artifacts": []},
            {"id": "probe", "title": "Probe", "risk_class": "r1",
             "depends_on": ["scope-review"], "required_artifacts": ["scope-notes"]},
        ],
        "gates": [
            {"id": "scoped", "name": "Scope reviewed",
             "required_artifacts": ["scope-notes"]}
        ],
    }
    template_path = home / "quick-recon.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")

    assert main(["--db", database, "playbook", "install", str(template_path)]) == 0
    assert "quick-recon" in capsys.readouterr().out

    # duplicate install rejected
    assert main(["--db", database, "playbook", "install", str(template_path)]) == 2
    assert "already installed" in capsys.readouterr().err

    assert main(["--db", database, "playbook", "list-custom"]) == 0
    assert "quick-recon" in capsys.readouterr().out

    assert (
        main(["--db", database, "playbook", "use", operation_id, "quick-recon"]) == 0
    )
    output = capsys.readouterr().out
    assert "scope-review" in output and "scoped" in output

    # unknown playbook
    assert main(["--db", database, "playbook", "use", operation_id, "missing"]) == 2
    assert "not found" in capsys.readouterr().err

    # invalid template rejected without side effects
    bad_path = home / "bad.json"
    bad_path.write_text(
        json.dumps({"name": "bad", "title": "Bad", "tasks": [
            {"id": "a", "title": "A", "depends_on": ["b"]},
            {"id": "b", "title": "B", "depends_on": ["a"]},
        ], "gates": []}),
        encoding="utf-8",
    )
    assert main(["--db", database, "playbook", "install", str(bad_path)]) == 2


def setup_agent_operation(database: str, llm_url: str, capsys) -> str:
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Memory agent",
                "--mode", "bug-bounty", "--include", "example.com",
            ]
        )
        == 0
    )
    operation_id = json.loads(capsys.readouterr().out)["id"]
    plan = {
        "title": "plan",
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
    assert main(["--db", database, "task", "start", operation_id, "recon"]) == 0
    capsys.readouterr()
    # the completion gate wants evidence; the scripted executor runs no tools
    assert (
        main(["--db", database, "artifact", "add", operation_id,
              "--kind", "note", "--text", "seed", "--task", "recon"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(["--db", database, "artifact", "add", operation_id,
              "--kind", "note", "--text", "seed", "--task", "testing"])
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


def approve_model(database: str, operation_id: str, capsys) -> str:
    assert (
        main(["--db", database, "agent", "run", operation_id, "recon",
              "--model", "fake/agent@v1"])
        == 2
    )
    error = capsys.readouterr().err
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]
    assert (
        main(["--db", database, "approval", "approve", approval_id,
              "--actor", "operator"])
        == 0
    )
    capsys.readouterr()
    return approval_id


def test_cli_agent_run_with_workspace_and_skill(
    home: Path, capsys, llm_server: str
) -> None:
    database = str(home / "agent-memory.db")
    operation_id = setup_agent_operation(database, llm_server, capsys)

    # глобальный секрет не должен попасть в prompt
    assert (
        main(["--db", database, "secret", "add", "global-token",
              "--value", "supersecret-token-value"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            ["--db", database, "memory", "add", "acme-bb",
             "Token rotation decision", "--kind", "decision",
             "--body", "rotated token supersecret-token-value after leak"]
        )
        == 0
    )
    capsys.readouterr()
    skill_file = home / "recon.md"
    skill_file.write_text("Always log the source of each asset.", encoding="utf-8")
    assert (
        main(["--db", database, "skill", "install", "recon-log", str(skill_file),
              "--trusted"])
        == 0
    )
    capsys.readouterr()

    approval_id = approve_model(database, operation_id, capsys)
    with LOCK:
        RESPONSES.extend([PLANNER_ANSWER, EXECUTOR_ANSWER])
    assert (
        main(
            ["--db", database, "agent", "run", operation_id, "recon",
             "--model", "fake/agent@v1", "--approval-id", approval_id,
             "--workspace", "acme-bb", "--skill", "recon-log"]
        )
        == 0
    )
    run_id = capsys.readouterr().out.split("\t")[0]

    with LOCK:
        prompts = list(PROMPTS)
    planner_prompt = prompts[0]
    assert "Campaign memory" in planner_prompt
    assert "Token rotation decision" in planner_prompt
    assert "supersecret-token-value" not in planner_prompt
    executor_prompt = prompts[1]
    assert "Installed skills" in executor_prompt
    assert "recon-log" in executor_prompt
    assert "Always log the source of each asset" in executor_prompt

    assert main(["--db", database, "--json", "agent", "show", run_id]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed"

    # аудит: workspace/skills зафиксированы в событии запуска
    assert (
        main(["--db", database, "--json", "operation", "show", operation_id,
              "--events"])
        == 0
    )
    shown = json.loads(capsys.readouterr().out)
    start = next(
        e for e in shown["events"]
        if e["event_type"] == "agent.run.start" and e["payload"]["run_id"] == run_id
    )
    assert start["payload"]["workspace"] == "acme-bb"
    assert start["payload"]["skills"] == ["recon-log"]
    assert len(start["payload"]["memory_entry_ids"]) == 1
    assert len(start["payload"]["skill_ids"]) == 1


def test_cli_swarm_with_workspace_and_skill(home: Path, capsys, llm_server: str) -> None:
    database = str(home / "swarm-memory.db")
    operation_id = setup_agent_operation(database, llm_server, capsys)
    assert (
        main(["--db", database, "memory", "add", "acme-bb", "Methodology v1",
              "--body", "breadth-first"])
        == 0
    )
    capsys.readouterr()
    skill_file = home / "s.md"
    skill_file.write_text("Keep payloads minimal.", encoding="utf-8")
    assert (
        main(["--db", database, "skill", "install", "minimal", str(skill_file),
              "--trusted"])
        == 0
    )
    capsys.readouterr()

    assert (
        main(["--db", database, "swarm", "run", operation_id,
              "--model", "fake/agent@v1"])
        == 2
    )
    error = capsys.readouterr().err
    approval_id = error.split("samurai approval approve ", 1)[1].split("'", 1)[0]
    assert (
        main(["--db", database, "approval", "approve", approval_id,
              "--actor", "operator"])
        == 0
    )
    capsys.readouterr()

    # recon уже started в setup; swarm по обеим задачам: recon in-progress → явный
    # выбор только testing + авто не подходит, проверяем passthrough на testing
    with LOCK:
        RESPONSES.extend([PLANNER_ANSWER, EXECUTOR_ANSWER])
    assert (
        main(
            ["--db", database, "swarm", "run", operation_id,
             "--model", "fake/agent@v1", "--task", "testing",
             "--approval-id", approval_id,
             "--workspace", "acme-bb", "--skill", "minimal"]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--db", database, "--json", "agent", "list", operation_id]) == 0
    runs = json.loads(capsys.readouterr().out)
    member = next(run for run in runs if run["task_id"] == "testing")
    assert member["workspace"] == "acme-bb"
    assert member["skills"] == ["minimal"]

    with LOCK:
        prompts = list(PROMPTS)
    assert any("Methodology v1" in prompt for prompt in prompts)
    assert any("Keep payloads minimal" in prompt for prompt in prompts)
