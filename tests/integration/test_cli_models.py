import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.cli import main

TEST_KEY = "ab" * 32
API_KEY = "cli-fake-key-777"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models" and self._authorized():
            self._reply(200, {"data": [{"id": "fake-model"}]})
        else:
            self._reply(401, {"error": {"message": "unauthorized"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length))
        if not self._authorized():
            return self._reply(401, {"error": {"message": "unauthorized"}})
        content = "cli:" + request["messages"][-1]["content"]
        self._reply(
            200,
            {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {API_KEY}"

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
def fake_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
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


def create_operation(database: str, capsys) -> str:
    assert (
        main(
            [
                "--db", database, "--json", "operation", "create", "Models CLI",
                "--mode", "ctf-htb", "--include", "example.com",
            ]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)["id"]


def register_fake(database: str, base_url: str, capsys) -> None:
    assert (
        main(
            [
                "--db", database, "model", "register", "fake",
                "--base-url", f"{base_url}/v1", "--secret-name", "fake-key",
            ]
        )
        == 0
    )
    capsys.readouterr()


def add_secret(database: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(API_KEY))
    assert main(["--db", database, "secret", "add", "fake-key", "--stdin"]) == 0


def test_cli_model_register_list_health(
    home: Path, capsys, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = str(home / "models.db")
    register_fake(database, fake_server, capsys)

    assert main(["--db", database, "model", "providers"]) == 0
    output = capsys.readouterr().out
    assert "openai" in output
    assert "ollama" in output
    assert "fake" in output

    assert main(["--db", database, "--json", "model", "list"]) == 0
    models = json.loads(capsys.readouterr().out)
    ids = {model["id"] for model in models}
    assert "openai/gpt-4o@v1" in ids
    assert "deepseek/deepseek-chat@v1" in ids

    assert main(["--db", database, "model", "health", "fake"]) == 2
    assert "not configured" in capsys.readouterr().err

    add_secret(database, monkeypatch)
    assert main(["--db", database, "model", "health", "fake"]) == 0
    assert "healthy" in capsys.readouterr().out

    assert main(["--db", database, "model", "remove", "fake"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "model", "health", "fake"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_model_complete_approval_flow(
    home: Path, capsys, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = str(home / "complete.db")
    operation_id = create_operation(database, capsys)
    register_fake(database, fake_server, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO(API_KEY))
    assert main(["--db", database, "secret", "add", "fake-key", "--stdin"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db", database, "model", "complete", "fake/fake-model@v1",
                "--operation", operation_id, "--prompt", "hello cli",
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
                "--db", database, "--json", "model", "complete", "fake/fake-model@v1",
                "--operation", operation_id, "--prompt", "hello cli",
                "--approval-id", approval_id,
            ]
        )
        == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response["text"] == "cli:hello cli"
    assert response["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "cached_tokens": 0,
    }

    assert (
        main(["--db", database, "--json", "operation", "show", operation_id, "--events"])
        == 0
    )
    events = json.loads(capsys.readouterr().out)["events"]
    calls = [event for event in events if event["event_type"] == "model.call"]
    assert len(calls) == 1
    assert calls[0]["payload"]["prompt_tokens"] == 4
    assert calls[0]["payload"]["model_id"] == "fake/fake-model@v1"
    assert API_KEY not in json.dumps(events)


def test_cli_model_complete_unknown_model_fails(home: Path, capsys) -> None:
    database = str(home / "unknown.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(
            [
                "--db", database, "model", "complete", "fake/missing@v1",
                "--operation", operation_id, "--prompt", "x",
            ]
        )
        == 2
    )
    assert "not found" in capsys.readouterr().err


def test_cli_model_fallback_budget_usage(
    home: Path, capsys, fake_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = str(home / "fallback.db")
    operation_id = create_operation(database, capsys)
    assert (
        main(
            [
                "--db", database, "model", "register", "broken",
                "--base-url", f"{fake_server}/v1", "--secret-name", "broken-key",
            ]
        )
        == 0
    )
    register_fake(database, fake_server, capsys)
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong-key"))
    assert main(["--db", database, "secret", "add", "broken-key", "--stdin"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(API_KEY))
    assert main(["--db", database, "secret", "add", "fake-key", "--stdin"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db", database, "model", "complete", "broken/fake-model@v1",
                "--operation", operation_id, "--prompt", "route me",
                "--fallback", "fake/fake-model@v1",
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
                "--db", database, "model", "complete", "broken/fake-model@v1",
                "--operation", operation_id, "--prompt", "route me",
                "--fallback", "fake/fake-model@v1", "--approval-id", approval_id,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.startswith("cli:route me")
    assert "broken/fake-model@v1 -> fake/fake-model@v1" in captured.err

    assert (
        main(["--db", database, "--json", "operation", "show", operation_id, "--events"])
        == 0
    )
    events = json.loads(capsys.readouterr().out)["events"]
    fallbacks = [event for event in events if event["event_type"] == "model.fallback"]
    assert fallbacks[0]["payload"]["reason"] == "auth"

    assert (
        main(["--db", database, "model", "budget", operation_id, "--max-calls", "1"]) == 0
    )
    capsys.readouterr()
    assert main(["--db", database, "model", "budget", operation_id]) == 0
    assert "max-calls=1" in capsys.readouterr().out

    approval = main(
        [
            "--db", database, "model", "complete", "fake/fake-model@v1",
            "--operation", operation_id, "--prompt", "blocked",
        ]
    )
    assert approval == 2
    assert "budget exhausted" in capsys.readouterr().err

    assert main(["--db", database, "--json", "model", "usage", operation_id]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["calls"] == 1
    assert summary["prompt_tokens"] == 4
    assert summary["by_model"]["fake/fake-model@v1"]["calls"] == 1


def test_cli_model_add_and_remove_custom_model(home: Path, capsys) -> None:
    database = str(home / "custom-model.db")

    assert main(["--db", database, "model", "add", "moonshot/kimi-k2-0905-preview"]) == 0
    assert "added\tmoonshot/kimi-k2-0905-preview@v1" in capsys.readouterr().out

    assert (
        main(
            ["--db", database, "--json", "model", "list", "--provider", "moonshot"]
        )
        == 0
    )
    ids = {model["id"] for model in json.loads(capsys.readouterr().out)}
    assert "moonshot/kimi-k2-0905-preview@v1" in ids
    assert "moonshot/kimi-k2@v1" in ids

    assert main(["--db", database, "model", "add", "moonshot/kimi-k2-0905-preview"]) == 2
    assert "already exists" in capsys.readouterr().err

    assert main(["--db", database, "model", "add", "moonshot/kimi-k2@v1"]) == 2
    assert "already registered" in capsys.readouterr().err

    assert main(["--db", database, "model", "remove", "moonshot/kimi-k2-0905-preview"]) == 0
    capsys.readouterr()
    assert (
        main(
            ["--db", database, "--json", "model", "list", "--provider", "moonshot"]
        )
        == 0
    )
    ids = {model["id"] for model in json.loads(capsys.readouterr().out)}
    assert "moonshot/kimi-k2-0905-preview@v1" not in ids

    assert main(["--db", database, "model", "remove", "moonshot/kimi-k2-0905-preview"]) == 2
    assert "not found" in capsys.readouterr().err


def test_cli_model_register_insecure(home: Path, capsys, fake_server: str) -> None:
    database = str(home / "insecure.db")
    assert (
        main(
            [
                "--db", database, "model", "register", "mitm",
                "--base-url", f"{fake_server}/v1", "--insecure",
            ]
        )
        == 0
    )
    assert "tls=off" in capsys.readouterr().out

    assert main(["--db", database, "--json", "model", "providers"]) == 0
    providers = json.loads(capsys.readouterr().out)
    row = next(entry for entry in providers if entry["id"] == "mitm")
    assert row["verify_tls"] is False

    assert main(["--db", database, "model", "providers"]) == 0
    assert "tls=off" in capsys.readouterr().out


def test_cli_model_add_slashed_name_on_custom_endpoint(
    home: Path, capsys, fake_server: str
) -> None:
    database = str(home / "slashed.db")
    register_fake(database, fake_server, capsys)

    assert (
        main(
            [
                "--db", database, "model", "add",
                "fake/giga-osa/deepseek-v4.1-flash-uncensored",
                "--display-name", "Giga DeepSeek", "--context-limit", "64000",
            ]
        )
        == 0
    )
    assert "added\tfake/giga-osa/deepseek-v4.1-flash-uncensored@v1" in (
        capsys.readouterr().out
    )

    assert main(["--db", database, "--json", "model", "list"]) == 0
    entries = json.loads(capsys.readouterr().out)
    added = next(
        entry
        for entry in entries
        if entry["id"] == "fake/giga-osa/deepseek-v4.1-flash-uncensored@v1"
    )
    assert added["provider_id"] == "fake"
    assert added["context_limit"] == 64000
    assert "tools" in added["capabilities"]
