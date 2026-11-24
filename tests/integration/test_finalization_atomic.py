from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai import cli
from samurai_ai.domain.graph import NodeState
from samurai_ai.domain.kernel import ArtifactKind, FindingSeverity, PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.finalization import FinalizationService
from samurai_ai.services.graph import GraphService
from samurai_ai.storage.graph import GraphRepository


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Atomic finalization lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("target.internal",)),
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    kernel.start_task(operation.id, "recon")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Origin")
    intent = graph.open_intent(operation.id, "recon", "Injection", root.id)
    graph.start_intent(intent.id)
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "differential proof", task_id="recon"
    )
    service = FinalizationService(operations, kernel, graph, services[15])
    return operation.id, intent.id, artifact.id, graph, kernel, service


def test_record_then_close_rolls_back_the_fact_when_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, intent_id, artifact_id, graph, _kernel, service = _state(tmp_path, monkeypatch)
    before = {node.id for node in graph.facts_of(intent_id)}

    def fail_close(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected close failure")

    monkeypatch.setattr(graph, "close_intent", fail_close)
    with pytest.raises(RuntimeError, match="injected close failure"):
        service.record_result_and_close(
            operation_id,
            intent_id,
            summary="new positive proof",
            artifact_ids=(artifact_id,),
            outcome="done",
        )

    assert {node.id for node in graph.facts_of(intent_id)} == before
    assert graph.get(intent_id).state is NodeState.RUNNING


def test_register_finding_rolls_back_all_writes_when_attachment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, intent_id, artifact_id, graph, kernel, service = _state(tmp_path, monkeypatch)
    fact = graph.record_fact(intent_id, "positive proof", (artifact_id,))
    graph.close_intent(intent_id, "done")
    before_events = len(kernel.operations.events(operation_id))

    def fail_attach(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected graph attachment failure")

    monkeypatch.setattr(graph, "attach_finding", fail_attach)
    with pytest.raises(RuntimeError, match="injected graph attachment failure"):
        service.register_finding(
            operation_id,
            fact.id,
            title="Blind injection",
            severity=FindingSeverity.HIGH,
        )

    assert kernel.kernel.list_findings(operation_id) == []
    assert len(kernel.operations.events(operation_id)) == before_events


def test_a_failed_nested_operation_does_not_erase_an_independent_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id, intent_id, artifact_id, graph, kernel, service = _state(tmp_path, monkeypatch)
    fact = graph.record_fact(intent_id, "positive proof", (artifact_id,))
    graph.close_intent(intent_id, "done")
    database = kernel.operations.repository.database

    def fail_attach(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected graph attachment failure")

    monkeypatch.setattr(graph, "attach_finding", fail_attach)
    with database.transaction():
        with pytest.raises(RuntimeError, match="injected graph attachment failure"):
            service.register_finding(operation_id, fact.id, title="Blind injection")
        # The independent artifact predates the failed savepoint and survives.
        assert kernel.kernel.get_artifact(artifact_id) is not None

    assert kernel.kernel.get_artifact(artifact_id) is not None
    assert kernel.kernel.list_findings(operation_id) == []
