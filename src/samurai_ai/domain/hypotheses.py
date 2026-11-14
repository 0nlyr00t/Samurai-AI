"""One completion contract for graph intents and CTF hypotheses.

Both registries represent attack hypotheses. Treating only one of them, or
counting ``testing`` as closed, lets an operation stop with real work left.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from samurai_ai.domain.ctf import Hypothesis, HypothesisStatus
from samurai_ai.domain.graph import GraphNode, NodeKind, NodeState

_GRAPH_OPEN = frozenset({NodeState.OPEN, NodeState.RUNNING, NodeState.PAUSED})
_GRAPH_CLOSED = frozenset(
    {NodeState.DONE, NodeState.BLOCKED, NodeState.EXHAUSTED, NodeState.DELETED}
)
_CTF_CLOSED = frozenset(
    {HypothesisStatus.CONFIRMED, HypothesisStatus.REFUTED, HypothesisStatus.ABANDONED}
)


@dataclass(frozen=True)
class HypothesisCoverage:
    total: int
    closed: int
    open_ids: tuple[str, ...]
    testing_ids: tuple[str, ...]

    @property
    def unfinished_ids(self) -> tuple[str, ...]:
        return (*self.open_ids, *self.testing_ids)

    @property
    def unfinished(self) -> int:
        return len(self.unfinished_ids)

    @property
    def complete(self) -> bool:
        return self.unfinished == 0


def hypothesis_coverage(
    graph_nodes: Iterable[GraphNode] = (), hypotheses: Iterable[Hypothesis] = ()
) -> HypothesisCoverage:
    """Count every live direction; closed requires a terminal, reasoned state."""
    total = 0
    closed = 0
    open_ids: list[str] = []
    testing_ids: list[str] = []
    for node in graph_nodes:
        if node.kind is not NodeKind.INTENT:
            continue
        if node.state in _GRAPH_OPEN:
            total += 1
            if node.state is NodeState.OPEN:
                open_ids.append(node.id)
            else:
                testing_ids.append(node.id)
        elif node.state in _GRAPH_CLOSED:
            total += 1
            closed += 1
    for hypothesis in hypotheses:
        total += 1
        if hypothesis.status is HypothesisStatus.OPEN:
            open_ids.append(hypothesis.id)
        elif hypothesis.status is HypothesisStatus.TESTING:
            testing_ids.append(hypothesis.id)
        elif hypothesis.status in _CTF_CLOSED:
            closed += 1
        else:  # a future nonterminal status must fail open, never look complete
            testing_ids.append(hypothesis.id)
    return HypothesisCoverage(total, closed, tuple(open_ids), tuple(testing_ids))


__all__ = ["HypothesisCoverage", "hypothesis_coverage"]
