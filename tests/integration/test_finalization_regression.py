"""Sanitized regression for a proven result that could not be finalized.

The fixture mirrors the state transitions of a real operation without naming its
target or making network calls: two closed intents, four unresolved intents, an
artifact-backed fact, a bare fact, and a legacy plan task already marked done.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from samurai_ai import cli
from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.goal import GoalState
from samurai_ai.domain.graph import EdgeRel, NodeState
from samurai_ai.domain.kernel import (
    ArtifactKind,
    FindingSeverity,
    PlanTask,
    TaskEvidenceError,
    TaskStatus,
)
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.goals import GoalService
from samurai_ai.services.graph import GraphService
from samurai_ai.storage.graph import GraphRepository


def _finished_run(graph: GraphService, operation_id: str, **parameters: object) -> str:
    """A tool run the harness executed: the only thing besides a bound artifact
    that confirms a fact."""
    run = ToolRun(
        operation_id=operation_id,
        tool_id="process.exec",
        tool_version=1,
        parameters=dict(parameters),
        status=ToolRunStatus.COMPLETED,
        exit_code=0,
    )
    graph.runs.add_run(
        run,
        OperationEvent(
            operation_id=operation_id, event_type="tool.run.completed", payload={"run_id": run.id}
        ),
    )
    return run.id


@dataclass
class RegressionState:
    operation_id: str
    services: tuple[object, ...]
    graph: GraphService
    goals: GoalService
    intents: dict[str, str]
    facts: dict[str, str]
    artifacts: dict[str, str]


def _legacy_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RegressionState:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_LOOT_DIR", str(tmp_path / "loot"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Sanitized finalization lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("target.internal",)),
        objective="Finish with evidence",
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    kernel.approve_plan(operation.id)
    kernel.start_task(operation.id, "recon")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Initial evidence")
    intents = {
        key: graph.open_intent(operation.id, "recon", title, root.id).id
        for key, title in {
            "file_read": "File read through the injection",
            "oracle": "Boolean oracle",
            "schema": "Extract schema",
            "context": "Request context",
            "fingerprint": "Database fingerprint",
            "credentials": "Credential tables",
        }.items()
    }
    artifacts = {
        "proof": kernel.register_artifact(
            operation.id,
            ArtifactKind.COMMAND_OUTPUT,
            "true and false responses are stable",
            task_id="recon",
        ).id,
        "fingerprint": kernel.register_artifact(
            operation.id,
            ArtifactKind.LOG,
            "database and current user fingerprint",
            task_id="recon",
        ).id,
    }
    file_read = graph.start_intent(intents["file_read"])
    # a negative result proven by the run that checked it, with no artifact
    bare_fact = graph.record_fact(
        file_read.id,
        "file functions are denied",
        negative=True,
        run_ids=(_finished_run(graph, operation.id, command="probe file functions"),),
    )
    graph.close_intent(file_read.id, "done")
    oracle = graph.start_intent(intents["oracle"])
    proof_fact = graph.record_fact(
        oracle.id, "boolean injection is proven", (artifacts["proof"],)
    )
    graph.close_intent(oracle.id, "done")
    fingerprint = graph.start_intent(intents["fingerprint"])
    fingerprint_fact = graph.record_fact(
        fingerprint.id, "database fingerprint extracted", (artifacts["fingerprint"],)
    )
    graph.start_intent(intents["schema"])
    graph.start_intent(intents["credentials"])
    # Force creates the historical legacy state; the new guard must not allow
    # fresh agent-driven completion to reach it.
    kernel.complete_task(operation.id, "recon", force=True)
    goals = services[15]
    return RegressionState(
        operation_id=operation.id,
        services=services,
        graph=graph,
        goals=goals,
        intents=intents,
        facts={
            "bare": bare_fact.id,
            "proof": proof_fact.id,
            "fingerprint": fingerprint_fact.id,
        },
        artifacts=artifacts,
    )


def test_fixture_has_the_sanitized_legacy_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _legacy_state(tmp_path, monkeypatch)
    unresolved = {
        state.intents["schema"],
        state.intents["context"],
        state.intents["fingerprint"],
        state.intents["credentials"],
    }
    assert all(state.graph.get(node_id).open for node_id in unresolved)
    assert state.graph.get(state.facts["proof"]).payload["artifact_ids"]
    assert not state.graph.get(state.facts["bare"]).payload["artifact_ids"]


def test_legacy_finding_uses_existing_fact_without_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import FinalizationService

    state = _legacy_state(tmp_path, monkeypatch)
    kernel = state.services[1]
    before_plan = kernel.get_current_plan(state.operation_id)
    service = FinalizationService(state.services[0], kernel, state.graph, state.goals)

    finding = service.register_finding(
        state.operation_id,
        state.facts["proof"],
        title="Blind SQL injection",
        severity=FindingSeverity.HIGH,
        summary="Stable differential and time-based oracles",
    )

    assert finding.evidence_ids == (state.artifacts["proof"],)
    assert kernel.get_current_plan(state.operation_id) == before_plan
    assert len(state.graph.graph.targets(state.facts["proof"], EdgeRel.PROVES.value)) == 1


def test_legacy_intent_can_close_without_a_new_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import FinalizationService

    state = _legacy_state(tmp_path, monkeypatch)
    kernel = state.services[1]
    before_plan = kernel.get_current_plan(state.operation_id)
    service = FinalizationService(state.services[0], kernel, state.graph, state.goals)

    fact = service.record_result_and_close(
        state.operation_id,
        state.intents["fingerprint"],
        summary="Database and current user confirmed",
        artifact_ids=(state.artifacts["fingerprint"],),
        outcome="done",
    )

    assert fact.payload["artifact_ids"] == [state.artifacts["fingerprint"]]
    assert state.graph.get(state.intents["fingerprint"]).state is NodeState.DONE
    assert kernel.get_current_plan(state.operation_id) == before_plan


def test_snapshot_names_eligible_facts_and_unresolved_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.finalization import FinalizationService

    state = _legacy_state(tmp_path, monkeypatch)
    service = FinalizationService(state.services[0], state.services[1], state.graph, state.goals)

    snapshot = service.snapshot(state.operation_id)

    assert state.facts["proof"] in snapshot.eligible_fact_ids
    assert state.facts["fingerprint"] in snapshot.eligible_fact_ids
    assert state.facts["bare"] not in snapshot.eligible_fact_ids
    assert set(snapshot.unresolved_ids) == {
        state.intents["schema"],
        state.intents["context"],
        state.intents["fingerprint"],
        state.intents["credentials"],
    }


def test_open_intent_blocks_fresh_task_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Guard lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("target.internal",))
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    kernel.start_task(operation.id, "recon")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Origin")
    intent = graph.open_intent(operation.id, "recon", "Open direction", root.id)

    with pytest.raises(TaskEvidenceError, match="intent"):
        kernel.complete_task(operation.id, "recon", no_finding="not checked")

    task = kernel.get_current_plan(operation.id).task_by_id("recon")
    assert task.status is TaskStatus.IN_PROGRESS
    assert intent.open


def test_evidence_task_can_close_with_positive_facts_and_no_vulnerability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Document answer lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("docs.internal",))
    )
    kernel.create_plan(
        operation.id,
        "Documentation review",
        [PlanTask(id="answer", title="Answer the documented question", outcome="evidence")],
        [],
    )
    kernel.start_task(operation.id, "answer")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "answer", "Documentation")
    intent = graph.open_intent(operation.id, "answer", "Read the API reference", root.id)
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "documented API response", task_id="answer"
    )
    graph.start_intent(intent.id)
    graph.record_fact(intent.id, "the documented retry intervals are 1m, 5m, 30m", (artifact.id,))
    graph.close_intent(intent.id, "done")

    plan = kernel.complete_task(operation.id, "answer", no_finding="no vulnerability sought")

    assert plan.task_by_id("answer").status is TaskStatus.DONE

    kernel.reset_task(operation.id, "answer")
    kernel.start_task(operation.id, "answer")
    plan = kernel.complete_task(operation.id, "answer")
    assert plan.task_by_id("answer").status is TaskStatus.DONE


def test_no_finding_cannot_override_positive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Guard lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("target.internal",))
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    kernel.start_task(operation.id, "recon")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Origin")
    intent = graph.open_intent(operation.id, "recon", "Injection", root.id)
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "differential proof", task_id="recon"
    )
    graph.start_intent(intent.id)
    graph.record_fact(intent.id, "positive differential proof", (artifact.id,))
    graph.close_intent(intent.id, "done")

    with pytest.raises(TaskEvidenceError, match="finding|evidence"):
        kernel.complete_task(operation.id, "recon", no_finding="nothing found")


def test_no_finding_is_allowed_after_an_exhausted_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, kernel = services[0], services[1]
    operation = operations.create(
        "Negative result lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("target.internal",)),
    )
    kernel.create_plan(operation.id, "Investigation", [PlanTask(id="recon", title="Recon")], [])
    kernel.start_task(operation.id, "recon")
    graph = GraphService(operations, kernel, GraphRepository(operations.repository.database))
    root = graph.origin(operation.id, "recon", "Origin")
    intent = graph.open_intent(operation.id, "recon", "Unavailable vector", root.id)
    artifact = kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "checked and denied", task_id="recon"
    )
    graph.start_intent(intent.id)
    graph.record_fact(intent.id, "vector denied", (artifact.id,), negative=True)
    graph.close_intent(intent.id, "exhausted", "all checks were negative")

    plan = kernel.complete_task(operation.id, "recon", no_finding="no exploitable vector")

    assert plan.task_by_id("recon").status is TaskStatus.DONE


def test_budget_cap_does_not_extend_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    services = cli._make_services(None)
    operations, goals = services[0], services[15]
    operation = operations.create(
        "Budget lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("target.internal",))
    )
    goal = goals.create(operation.id, "Bounded work", budget_steps=1)

    exhausted = goals.record_step(goal.id)

    assert exhausted.state is GoalState.FINALIZING
    assert exhausted.budget_steps == 1
    assert not [
        event
        for event in operations.events(operation.id)
        if event.event_type == "goal.budget-extended"
    ]
