"""The acquisition path end to end: doc → draft → review → install → run.

Local HTTP server, fake model through ModelService, fake package manager: no
real network, no real install. The container-only and hand-written shapes are
covered too, because install carries both of them through one boundary.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.agents.provider import ChatRequest, ChatResponse
from samurai_ai.agents.registry import ModelRegistry
from samurai_ai.cli import main
from samurai_ai.domain.models import (
    ApiFamily,
    ModelCapability,
    ModelDefinition,
    ProviderDefinition,
)
from samurai_ai.execution.process import ProcessManager, ProcessResult
from samurai_ai.services.drafts import read_meta, sidecar_path
from samurai_ai.services.models import ModelService
from samurai_ai.services.provisioner import Provisioner

DIGEST = "0123456789abcdef" * 4
MODEL_ID = "fake/draft@v1"

DOC = f"""\
# samuraidemo

`samuraidemo` is a non-interactive scanner.

Debian: `sudo apt-get install -y samuraidemo`. macOS: `brew install samuraidemo`.

Container image: ghcr.io/acme/samuraidemo@sha256:{DIGEST}

Verify the install with `samuraidemo --version`.

Example: `samuraidemo --proxy 127.0.0.1:8080 -t example.com`

Keys: api_key=supersecretvalue
"""

CANDIDATE: dict[str, Any] = {
    "id": "scan.samuraidemo",
    "title": "Samurai demo scan",
    "description": "Non-interactive demo scanner",
    "image": f"ghcr.io/acme/samuraidemo@sha256:{DIGEST}",
    "executable": "samuraidemo",
    "risk_class": "r1",
    "network": "scoped",
    "read_only": True,
    "proxy_flag": "--proxy",
    "execution": {"native": True, "project_local": False, "container": True},
    "provision": {
        "packages": {
            "linux": {"manager": "apt", "package": "samuraidemo"},
            "macos": {"manager": "brew", "package": "samuraidemo"},
        },
        "healthcheck": ["--version"],
    },
    "params": {"target": {"type": "string", "required": True}},
    "targets": {"param": "target", "kind": "host", "max": 1},
    "argv": ["-t", "{target}"],
    "parser": "text-lines",
}


class _DocHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = DOC.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def docs_server() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _DocHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/doc"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class FakeProvider:
    """One canned answer, recorded so the request itself can be checked."""

    def __init__(self) -> None:
        self.answer = json.dumps(CANDIDATE)
        self.requests: list[ChatRequest] = []

    async def health(self) -> bool:
        return True

    async def list_models(self) -> list[str]:
        return []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(text=self.answer, model_id=request.model_id, provider_id="fake")


class FakePackages:
    """A package manager that always succeeds and a tool that always answers."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def __call__(self, argv: list[str], _timeout: float) -> ProcessResult:
        self.argv.append(list(argv))
        return ProcessResult(exit_code=0, stdout="", stderr="")


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = ProviderDefinition(
        id="fake",
        api_family=ApiFamily.OPENAI_COMPATIBLE,
        base_url="http://127.0.0.1/v1",
        local=True,
    )
    model = ModelDefinition(
        id=MODEL_ID,
        provider_id="fake",
        capabilities=frozenset({ModelCapability.STRUCTURED_OUTPUT}),
        local=True,
    )
    monkeypatch.setattr(ModelRegistry, "get_provider", lambda self, provider_id: provider)
    monkeypatch.setattr(ModelRegistry, "get_model", lambda self, model_id: (model, provider))
    fake = FakeProvider()
    monkeypatch.setattr(ModelService, "_make_provider", lambda self, _provider, *_args: fake)
    return fake


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    isolated = tmp_path / "home"
    monkeypatch.setenv("SAMURAI_HOME", str(isolated))
    return isolated


def _operation(database: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert (
        main(
            [
                "--db",
                database,
                "--json",
                "operation",
                "create",
                "Tool draft",
                "--mode",
                "bug-bounty",
                "--include",
                "127.0.0.1",
            ]
        )
        == 0
    )
    return str(json.loads(capsys.readouterr().out)["id"])


def _tool_list(database: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["--db", database, "tool", "list"]) == 0
    return capsys.readouterr().out


# --- the vertical slice --------------------------------------------------


def test_documentation_becomes_a_draft_then_a_trusted_tool(
    tmp_path: Path,
    home: Path,
    docs_server: str,
    fake_model: FakeProvider,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = str(tmp_path / "draft.db")
    operation = _operation(database, capsys)
    output = tmp_path / "drafts" / "scan.samuraidemo.json"

    # 1. draft: nothing installed, nothing registered
    code = main(
        [
            "--db",
            database,
            "tool",
            "draft",
            docs_server,
            "--out",
            str(output),
            "--operation",
            operation,
            "--model",
            MODEL_ID,
        ]
    )
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "status\tready" in out.out
    assert "review it, then: samurai tool install" in out.out
    assert not (home / "recipes").exists()
    assert not (home / "tools").exists()
    assert "scan.samuraidemo" not in _tool_list(database, capsys)

    # the draft is unconfirmed trust and the secret never reaches the file
    payload = json.loads(output.read_text())
    assert payload["trusted"] is False
    assert "supersecretvalue" not in output.read_text()
    meta = read_meta(output)
    assert meta is not None and meta.ready
    # the doc's api key was redacted before the model ever saw it
    assert meta.redactions >= 1
    assert meta.source_ref == docs_server
    assert fake_model.requests and "samuraidemo" in fake_model.requests[0].messages[0].content

    # the operator's review material: path, source, digest, content
    assert str(output) in out.out
    assert f"doc-sha256\t{meta.document_sha256}" in out.out
    assert f"candidate-sha256\t{meta.candidate_sha256}" in out.out
    assert '"id": "scan.samuraidemo"' in out.out

    # 2. install without --trusted changes nothing
    assert main(["--db", database, "tool", "install", str(output)]) == 2
    assert "needs --trusted" in capsys.readouterr().err
    assert not (home / "recipes").exists()

    # 3. an operator who read it installs it: the tool enters the catalogue
    assert main(["--db", database, "tool", "install", str(output), "--trusted"]) == 0
    assert "installed recipe\tscan.samuraidemo" in capsys.readouterr().out
    installed = home / "recipes" / "scan.samuraidemo.json"
    assert installed.is_file()
    assert (home / "recipes" / "scan.samuraidemo.sha256").is_file()
    assert "scan.samuraidemo" in _tool_list(database, capsys)

    # 4. the existing provision dry-run plans it from the recipe, still changing nothing
    assert main(["--db", database, "tool", "provision", "scan.samuraidemo", "--dry-run"]) == 0
    plan = capsys.readouterr().out
    assert "package   samuraidemo" in plan
    assert "dry-run: nothing was changed" in plan
    assert not (home / "tools").exists()

    # 5. approval → install → healthcheck, through a fake package manager
    packages = FakePackages()
    monkeypatch.setattr(
        "samurai_ai.cli_execution._provisioner",
        lambda operations: Provisioner(
            operations=operations,
            kernel=None,
            runner=packages,
            templates=tmp_path / "templates",
            tools_dir=home / "tools",
            state_file=tmp_path / "provision-state.json",
        ),
    )
    assert main(["--db", database, "tool", "provision", "scan.samuraidemo"]) == 0
    assert "ready\tscan.samuraidemo" in capsys.readouterr().out
    # the install argv came from the recipe, then the healthcheck proved it
    assert packages.argv[0][0] in ("brew", "apt-get")
    assert packages.argv[0][-1] == "samuraidemo"
    assert ["samuraidemo", "--version"] in packages.argv

    # 6. verify is read-only and reports the tool answering
    real_which = shutil.which
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/samuraidemo" if name == "samuraidemo" else real_which(name),
    )

    async def fake_run(self: ProcessManager, argv: list[str], **_kwargs: Any) -> ProcessResult:
        return ProcessResult(exit_code=0, stdout="samuraidemo 1.2.3", stderr="")

    monkeypatch.setattr(ProcessManager, "run", fake_run)
    assert main(["--db", database, "tool", "verify", "scan.samuraidemo"]) == 0
    assert "samuraidemo 1.2.3" in capsys.readouterr().out

    # 7. the governed run needs no new path
    code = main(
        [
            "--db",
            database,
            "tool",
            "run",
            operation,
            "scan.samuraidemo",
            "--param",
            "target=127.0.0.1",
        ]
    )
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "samuraidemo 1.2.3" in out.out


def test_an_incomplete_draft_cannot_be_installed(
    tmp_path: Path, home: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_model.answer = json.dumps({"id": "scan.halfbaked", "title": "Half"})
    database = str(tmp_path / "half.db")
    operation = _operation(database, capsys)
    doc = tmp_path / "doc.md"
    doc.write_text(f"samuraidemo sha256:{DIGEST}\n", encoding="utf-8")
    output = tmp_path / "scan.halfbaked.json"

    code = main(
        [
            "--db",
            database,
            "tool",
            "draft",
            str(doc),
            "--out",
            str(output),
            "--operation",
            operation,
        ]
    )
    out = capsys.readouterr()
    assert code == 1, out.err
    assert "status\tincomplete" in out.out
    assert "missing\tmissing required field: image" in out.out

    assert main(["--db", database, "tool", "install", str(output), "--trusted"]) == 2
    err = capsys.readouterr().err
    assert "cannot be installed yet" in err
    assert "missing required field: image" in err
    assert not (home / "recipes").exists()


def test_an_offline_draft_refuses_a_remote_source(
    tmp_path: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "offline.db")
    operation = _operation(database, capsys)
    code = main(
        [
            "--db",
            database,
            "tool",
            "draft",
            "https://example.com/doc",
            "--out",
            str(tmp_path / "out.json"),
            "--operation",
            operation,
            "--offline",
        ]
    )
    assert code == 2
    assert "offline" in capsys.readouterr().err
    assert fake_model.requests == []
    assert not (tmp_path / "out.json").exists()


def test_a_source_scheme_other_than_http_is_refused(
    tmp_path: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "scheme.db")
    operation = _operation(database, capsys)
    code = main(
        [
            "--db",
            database,
            "tool",
            "draft",
            "ftp://example.com/doc",
            "--out",
            str(tmp_path / "out.json"),
            "--operation",
            operation,
        ]
    )
    assert code == 2
    assert "scheme" in capsys.readouterr().err


def test_a_draft_needs_an_operation(
    tmp_path: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--db",
            str(tmp_path / "noop.db"),
            "tool",
            "draft",
            "x.md",
            "--out",
            str(tmp_path / "o.json"),
        ]
    )
    assert code == 2
    assert "no operation" in capsys.readouterr().err


def test_a_redraft_shows_the_diff_against_the_previous_file(
    tmp_path: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "redraft.db")
    operation = _operation(database, capsys)
    doc = tmp_path / "doc.md"
    doc.write_text(DOC, encoding="utf-8")
    output = tmp_path / "scan.samuraidemo.json"
    assert (
        main(
            ["--db", database, "tool", "draft", str(doc), "--out", str(output)]
            + ["--operation", operation]
        )
        == 0
    )
    capsys.readouterr()

    fake_model.answer = json.dumps({**CANDIDATE, "title": "Samurai demo scan, renamed"})
    assert (
        main(
            ["--db", database, "tool", "draft", str(doc), "--out", str(output)]
            + ["--operation", operation]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "# diff against the file that was already at this path" in out
    assert '-\t"title": "Samurai demo scan",' in out.replace("  ", "\t")


def test_an_edited_draft_is_still_installable_with_a_note(
    tmp_path: Path, home: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "edited.db")
    operation = _operation(database, capsys)
    doc = tmp_path / "doc.md"
    doc.write_text(DOC, encoding="utf-8")
    output = tmp_path / "scan.samuraidemo.json"
    assert (
        main(
            ["--db", database, "tool", "draft", str(doc), "--out", str(output)]
            + ["--operation", operation]
        )
        == 0
    )
    capsys.readouterr()

    payload = json.loads(output.read_text())
    payload["title"] = "Edited by the operator"
    output.write_text(json.dumps(payload))

    assert main(["--db", database, "tool", "install", str(output), "--trusted"]) == 0
    captured = capsys.readouterr()
    assert "edited after it was generated" in captured.err
    assert "installed recipe\tscan.samuraidemo" in captured.out


def test_a_container_only_draft_lands_in_the_manifest_store(
    tmp_path: Path, home: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_model.answer = json.dumps(
        {
            **CANDIDATE,
            "id": "scan.containeronly",
            "execution": {"native": False, "project_local": False, "container": True},
            "provision": {"packages": {}},
        }
    )
    database = str(tmp_path / "container.db")
    operation = _operation(database, capsys)
    doc = tmp_path / "doc.md"
    doc.write_text(DOC, encoding="utf-8")
    output = tmp_path / "scan.containeronly.json"
    assert (
        main(
            ["--db", database, "tool", "draft", str(doc), "--out", str(output)]
            + ["--operation", operation]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["--db", database, "tool", "install", str(output), "--trusted"]) == 0
    assert "installed\tscan.containeronly" in capsys.readouterr().out
    assert (home / "tools" / "scan.containeronly.json").is_file()
    assert not (home / "recipes").exists()
    assert main(["--db", database, "tool", "manifests"]) == 0
    assert "scan.containeronly" in capsys.readouterr().out


def test_a_recipe_shaped_file_without_a_sidecar_installs_as_before(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scan.plain.json"
    path.write_text(json.dumps(CANDIDATE))
    assert read_meta(path) is None
    database = str(tmp_path / "plain.db")

    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    assert "installed recipe\tscan.samuraidemo" in capsys.readouterr().out
    assert (home / "recipes" / "scan.samuraidemo.json").is_file()


def test_a_second_install_of_the_same_recipe_needs_replace(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scan.plain.json"
    path.write_text(json.dumps(CANDIDATE))
    database = str(tmp_path / "replace.db")

    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    capsys.readouterr()
    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 2
    assert "--replace" in capsys.readouterr().err
    assert main(["--db", database, "tool", "install", str(path), "--trusted", "--replace"]) == 0


def test_a_broken_recipe_is_refused_and_nothing_lands(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scan.broken.json"
    path.write_text(json.dumps({**CANDIDATE, "image": "busybox:latest"}))
    database = str(tmp_path / "broken.db")

    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 2
    assert "pinned by digest" in capsys.readouterr().err
    assert not (home / "recipes").exists()


def test_a_manifest_only_shape_stays_a_manifest(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recipe shape without native/project-local is refused by the catalogue."""
    payload = {
        **CANDIDATE,
        "id": "scan.nativeless",
        "execution": {"native": False, "project_local": False, "container": True},
    }
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(payload))
    directory = home / "recipes"
    from samurai_ai.services.catalog import CatalogError, RecipeCatalog, user_recipes_directory

    assert user_recipes_directory() == directory
    with pytest.raises(CatalogError, match="neither native nor project-local"):
        RecipeCatalog().install(path, trusted=True, directory=directory)
    assert not (directory / "scan.nativeless.json").exists()


def test_uninstall_removes_a_recipe(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "scan.plain.json"
    path.write_text(json.dumps(CANDIDATE))
    database = str(tmp_path / "uninstall-recipe.db")
    assert main(["--db", database, "tool", "install", str(path), "--trusted"]) == 0
    capsys.readouterr()

    assert main(["--db", database, "tool", "uninstall", "scan.samuraidemo"]) == 0
    assert "removed recipe\tscan.samuraidemo" in capsys.readouterr().out
    assert not (home / "recipes" / "scan.samuraidemo.json").exists()
    assert not (home / "recipes" / "scan.samuraidemo.sha256").exists()


def test_the_sidecar_sits_next_to_the_draft(tmp_path: Path) -> None:
    assert sidecar_path(tmp_path / "a.json").name == "a.json.draft.json"


def test_a_ready_draft_edited_into_a_broken_file_is_refused(
    tmp_path: Path, home: Path, fake_model: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    database = str(tmp_path / "edited-broken.db")
    operation = _operation(database, capsys)
    doc = tmp_path / "doc.md"
    doc.write_text(DOC, encoding="utf-8")
    output = tmp_path / "scan.samuraidemo.json"
    assert (
        main(
            ["--db", database, "tool", "draft", str(doc), "--out", str(output)]
            + ["--operation", operation]
        )
        == 0
    )
    capsys.readouterr()

    payload = json.loads(output.read_text())
    payload["image"] = "busybox:latest"
    output.write_text(json.dumps(payload))

    assert main(["--db", database, "tool", "install", str(output), "--trusted"]) == 2
    assert "pinned by digest" in capsys.readouterr().err
    assert not (home / "recipes" / "scan.samuraidemo.json").exists()
