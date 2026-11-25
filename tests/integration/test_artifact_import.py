from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai import cli
from samurai_ai.domain.kernel import ArtifactKind, PlanTask, TaskStateError
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.graph import GraphService
from samurai_ai.storage.graph import GraphRepository


def _services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, max_bytes: int = 1024):
    from samurai_ai.services.finalization import FinalizationService

    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Artifact import lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("target.internal",)),
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    service = FinalizationService(
        operations,
        kernel,
        graph,
        services[15],
        workspace=workspace,
        trusted_roots=(workspace,),
        max_artifact_bytes=max_bytes,
    )
    return operation.id, workspace, kernel, service


def test_import_accepts_relative_and_absolute_paths_and_redacts_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, workspace, kernel, service = _services(tmp_path, monkeypatch)
    relative = workspace / "evidence.txt"
    relative.write_text("token=private-value\npublic fact\n", encoding="utf-8")

    imported = service.import_artifact(
        operation_id,
        "evidence.txt",
        kind=ArtifactKind.LOG,
        summary="proof",
        task_id="recon",
    )
    absolute = service.import_artifact(
        operation_id,
        relative,
        kind=ArtifactKind.LOG,
        task_id="recon",
    )

    assert imported.id != absolute.id
    artifact_file = kernel.kernel.artifact_root / operation_id / f"{imported.id}.txt"
    text = artifact_file.read_text(encoding="utf-8")
    assert "private-value" not in text
    assert "***REDACTED***" in text
    assert "public fact" in text
    assert imported.task_id == "recon"
    assert imported.created_by == "agent"


def test_import_rejects_outside_trust_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import ArtifactImportError

    operation_id, workspace, _kernel, service = _services(tmp_path, monkeypatch)
    # Scratch roots differ by host; this case isolates the explicit trust boundary.
    service.trusted_roots = (workspace,)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ArtifactImportError, match="outside"):
        service.import_artifact(operation_id, outside)

    (workspace / "escape.txt").symlink_to(outside)
    with pytest.raises(ArtifactImportError, match="outside"):
        service.import_artifact(operation_id, "escape.txt")


def test_import_rejects_internal_state_even_inside_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import ArtifactImportError

    operation_id, workspace, _kernel, service = _services(tmp_path, monkeypatch)
    internal = workspace / ".config" / "samurai"
    internal.mkdir(parents=True)
    (internal / "evidence.txt").write_text("state", encoding="utf-8")

    with pytest.raises(ArtifactImportError, match="internal state"):
        service.import_artifact(operation_id, ".config/samurai/evidence.txt")


def test_import_enforces_the_binary_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import ArtifactImportError

    operation_id, workspace, _kernel, service = _services(tmp_path, monkeypatch, max_bytes=4)
    (workspace / "large.bin").write_bytes(b"12345")

    with pytest.raises(ArtifactImportError, match="too large"):
        service.import_artifact(operation_id, "large.bin")


def test_import_preserves_binary_evidence_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, workspace, kernel, service = _services(tmp_path, monkeypatch)
    payload = b"\x00\xff\x01\x02"
    (workspace / "proof.png").write_bytes(payload)

    artifact = service.import_artifact(operation_id, "proof.png")

    stored = kernel.kernel.artifact_root / operation_id / f"{artifact.id}.png"
    assert stored.read_bytes() == payload


def test_import_rejects_a_nonpositive_size_limit_and_invalid_utf8_is_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, workspace, kernel, service = _services(tmp_path, monkeypatch, max_bytes=4)
    with pytest.raises(ValueError, match="positive"):
        _services(tmp_path, monkeypatch, max_bytes=0)

    payload = b"\xff\xfe"
    (workspace / "binary.txt").write_bytes(payload)
    artifact = service.import_artifact(operation_id, "binary.txt")
    stored = kernel.kernel.artifact_root / operation_id / f"{artifact.id}.txt"
    assert stored.read_bytes() == payload


def test_import_validates_the_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, workspace, _kernel, service = _services(tmp_path, monkeypatch)
    (workspace / "evidence.txt").write_text("proof", encoding="utf-8")

    with pytest.raises(TaskStateError):
        service.import_artifact(operation_id, "evidence.txt", task_id="missing")


def _run(service, operation_id: str, status: str = "completed", **parameters: object) -> str:  # type: ignore[no-untyped-def]
    from samurai_ai.domain.execution import ToolRun, ToolRunStatus
    from samurai_ai.domain.operations import OperationEvent

    run = ToolRun(
        operation_id=operation_id,
        tool_id="process.exec",
        tool_version=1,
        parameters=dict(parameters),
        status=ToolRunStatus(status),
    )
    service.graph.runs.add_run(
        run, OperationEvent(operation_id=operation_id, event_type="tool.run.test", payload={})
    )
    return run.id


def test_import_binds_to_the_run_that_wrote_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.domain.evidence import artifact_is_bound
    from samurai_ai.services.finalization import ArtifactImportError

    operation_id, workspace, _kernel, service = _services(tmp_path, monkeypatch)
    loot = workspace / "loot.txt"
    loot.write_text("flag{example}\n", encoding="utf-8")
    writer = _run(service, operation_id, command=f"curl -o {loot} http://target.internal/")
    stranger = _run(service, operation_id, command="id")
    pending = _run(service, operation_id, "running", command=f"cat {loot}")

    unbound = service.import_artifact(operation_id, loot)
    bound = service.import_artifact(operation_id, loot, source_run_id=writer)

    assert not artifact_is_bound(unbound)
    assert bound.source_run_id == writer and artifact_is_bound(bound)
    with pytest.raises(ArtifactImportError, match="never touched"):
        service.import_artifact(operation_id, loot, source_run_id=stranger)
    with pytest.raises(ArtifactImportError, match="only a finished run"):
        service.import_artifact(operation_id, loot, source_run_id=pending)
    with pytest.raises(ArtifactImportError, match="unknown run"):
        service.import_artifact(operation_id, loot, source_run_id="run_missing")
