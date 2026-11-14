from samurai_ai.domain.ctf import Hypothesis, HypothesisStatus
from samurai_ai.domain.graph import GraphNode, NodeKind, NodeState
from samurai_ai.domain.hypotheses import hypothesis_coverage


def intent(node_id: str, state: NodeState) -> GraphNode:
    return GraphNode(
        id=node_id,
        operation_id="op",
        task_id="task",
        kind=NodeKind.INTENT,
        state=state,
    )


def hypothesis(hypothesis_id: str, status: HypothesisStatus) -> Hypothesis:
    return Hypothesis(id=hypothesis_id, operation_id="op", title=hypothesis_id, status=status)


def test_every_live_registry_state_is_unfinished() -> None:
    coverage = hypothesis_coverage(
        [
            intent("graph-open", NodeState.OPEN),
            intent("graph-running", NodeState.RUNNING),
            intent("graph-paused", NodeState.PAUSED),
        ],
        [
            hypothesis("ctf-open", HypothesisStatus.OPEN),
            hypothesis("ctf-testing", HypothesisStatus.TESTING),
        ],
    )

    assert coverage.total == 5
    assert coverage.closed == 0
    assert coverage.open_ids == ("graph-open", "ctf-open")
    assert coverage.testing_ids == ("graph-running", "graph-paused", "ctf-testing")
    assert not coverage.complete


def test_terminal_and_abandoned_states_are_closed() -> None:
    coverage = hypothesis_coverage(
        [
            intent("done", NodeState.DONE),
            intent("blocked", NodeState.BLOCKED),
            intent("exhausted", NodeState.EXHAUSTED),
            intent("deleted", NodeState.DELETED),
        ],
        [
            hypothesis("confirmed", HypothesisStatus.CONFIRMED),
            hypothesis("refuted", HypothesisStatus.REFUTED),
            hypothesis("abandoned", HypothesisStatus.ABANDONED),
        ],
    )

    assert coverage.total == 7
    assert coverage.closed == 7
    assert coverage.unfinished == 0
    assert coverage.complete


def test_non_intent_nodes_are_not_hypotheses() -> None:
    fact = GraphNode(
        operation_id="op", task_id="task", kind=NodeKind.FACT, state=NodeState.CONFIRMED
    )
    assert hypothesis_coverage([fact]).total == 0
