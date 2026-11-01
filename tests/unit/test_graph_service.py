"""The graph keeps its promises: every invariant has a test that bites."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.graph import (
    FRONTIER_MAX_CHARS,
    MAX_OPEN_INTENTS,
    GraphConflictError,
    GraphInvariantError,
    GraphLimitError,
    NodeKind,
    NodeState,
)
from samurai_ai.domain.kernel import ArtifactKind, PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.graph import GraphService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.graph import GraphRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository


@pytest.fixture
def stack(tmp_path: Path) -> tuple[GraphService, KernelService, object, str, str]:
    database = Database(tmp_path / "graph.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    graph = GraphService(operations, kernel, GraphRepository(database))
    operation = operations.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    plan = kernel.create_plan(
        operation.id,
        "plan",
        [PlanTask(id="recon", title="Recon"), PlanTask(id="second", title="Second")],
        [],
    )
    task, other = plan.tasks
    return graph, kernel, operations, operation.id, task.id, other.id
    

@pytest.fixture
def root(stack):
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    return graph.origin(operation_id, task_id, "gate lab")


def test_the_origin_is_idempotent(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack

    assert graph.origin(operation_id, task_id, "again").id == root.id
    assert [n.kind for n in graph.for_task(task_id)].count(NodeKind.ORIGIN) == 1


def test_an_intent_hangs_on_the_origin(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack

    intent = graph.open_intent(operation_id, task_id, "check /api/v1", root.id)

    assert intent.state is NodeState.OPEN
    assert [n.id for n in graph.lineage(intent.id)] == [root.id]


def test_an_intent_needs_a_confirmed_fact(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "first", root.id)

    with pytest.raises(GraphInvariantError):
        graph.open_intent(operation_id, task_id, "second", intent.id)  # intent is not a fact


def test_an_empty_title_is_refused(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack

    with pytest.raises(GraphInvariantError):
        graph.open_intent(operation_id, task_id, "   ", root.id)


def test_open_intents_are_capped(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    for index in range(MAX_OPEN_INTENTS):
        graph.open_intent(operation_id, task_id, f"direction {index}", root.id)

    with pytest.raises(GraphLimitError):
        graph.open_intent(operation_id, task_id, "one too many", root.id)


def test_done_needs_a_fact(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)

    with pytest.raises(GraphInvariantError):
        graph.close_intent(intent.id, "done")


def test_blocked_needs_a_reason(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)

    with pytest.raises(GraphInvariantError):
        graph.close_intent(intent.id, "blocked")


def test_a_fact_completes_an_intent(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)
    fact = graph.record_fact(intent.id, "12 endpoints, no auth on /api/v1/user")

    closed = graph.close_intent(intent.id, "done")

    assert closed.state is NodeState.DONE
    assert [n.id for n in graph.graph.sources(fact.id, "yields")] == [intent.id]


def test_a_fact_needs_a_running_intent(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)

    with pytest.raises(GraphInvariantError):
        graph.record_fact(intent.id, "too early")

    graph.start_intent(intent.id)
    assert graph.record_fact(intent.id, "now fine").state is NodeState.CONFIRMED


def test_unknown_artifacts_are_refused(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)

    with pytest.raises(GraphInvariantError):
        graph.record_fact(intent.id, "claims evidence", ("art_missing",))


def test_a_known_artifact_is_accepted(stack, root) -> None:
    graph, kernel, _operations, operation_id, task_id, _other = stack
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)

    fact = graph.record_fact(intent.id, "id incremental", (artifact.id,))

    assert fact.payload["artifact_ids"] == [artifact.id]


def test_a_finding_needs_a_fact_with_an_artifact(stack, root) -> None:
    graph, kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)
    bare = graph.record_fact(intent.id, "no artifact")

    with pytest.raises(GraphInvariantError):
        graph.attach_finding(bare.id, "fnd_1")

    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    proven = graph.record_fact(intent.id, "IDOR confirmed", (artifact.id,))
    assert graph.attach_finding(proven.id, "fnd_0").kind is NodeKind.FINDING


def test_a_finding_hangs_on_the_fact(stack, root) -> None:
    graph, kernel, _operations, operation_id, task_id, _other = stack
    artifact = kernel.register_artifact(operation_id, ArtifactKind.COMMAND_OUTPUT, "body")
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)
    fact = graph.record_fact(intent.id, "IDOR", (artifact.id,))

    finding = graph.attach_finding(fact.id, "fnd_1")

    assert finding.kind is NodeKind.FINDING
    assert [n.id for n in graph.graph.sources(finding.id, "proves")] == [fact.id]


def test_a_lost_race_is_a_conflict(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    stale = graph.get(intent.id)
    graph.start_intent(intent.id)

    with pytest.raises(GraphConflictError):
        graph.graph.transition(
            stale.id, stale.state, NodeState.DONE, now="2026-01-01T00:00:00+00:00"
        )


def test_a_task_with_open_intents_names_them(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "still open", root.id)

    blocking = graph.blocking_intents(task_id)
    assert [node.id for node in blocking] == [intent.id]

    graph.start_intent(intent.id)
    graph.close_intent(intent.id, "exhausted", "nothing there")
    assert graph.blocking_intents(task_id) == []


def test_a_hint_is_consumed_by_the_next_intent(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    hint = graph.add_hint(operation_id, task_id, "do not touch /admin")

    graph.open_intent(operation_id, task_id, "check public", root.id)

    assert graph.get(hint.id).state is NodeState.CONSUMED


def test_the_frontier_puts_hints_first_and_stays_bounded(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    graph.add_hint(operation_id, task_id, "do not touch /admin")
    intent = graph.open_intent(operation_id, task_id, "check public", root.id)
    graph.start_intent(intent.id)
    graph.record_fact(intent.id, "x" * 1900)

    block = graph.frontier(task_id)

    assert block.startswith("operator hints:")
    assert "do not touch /admin" in block
    assert "(no artifact)" in block
    assert len(block) <= FRONTIER_MAX_CHARS + 1


def test_dismissing_a_fact_is_one_way(stack, root) -> None:
    graph, _kernel, _operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)
    fact = graph.record_fact(intent.id, "noise")

    assert graph.dismiss(fact.id).state is NodeState.DISMISSED
    with pytest.raises(GraphConflictError):
        graph.dismiss(fact.id)


def test_an_intent_from_another_task_is_refused(stack, root) -> None:
    graph, kernel, _operations, operation_id, task_id, _other = stack
    with pytest.raises(GraphInvariantError):
        graph.open_intent(operation_id, _other, "look", root.id)


def test_every_mutation_is_journalled(stack, root) -> None:
    graph, _kernel, operations, operation_id, task_id, _other = stack
    intent = graph.open_intent(operation_id, task_id, "look", root.id)
    graph.start_intent(intent.id)

    types = [
        event.event_type
        for event in operations.repository.events(operation_id)
        if event.event_type.startswith("graph.")
    ]
    assert "graph.origin" in types
    assert "graph.intent.opened" in types
    assert "graph.state" in types
