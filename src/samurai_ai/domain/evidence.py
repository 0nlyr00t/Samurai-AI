"""What counts as proof — one definition for facts, tasks, findings and goals.

A model's sentence is a claim. Proof is something the harness itself produced
or can trace: a tool run it executed, an artifact that run wrote, or evidence the
operator supplied. Every completion check (an intent closing as ``done``, a task
completing, a goal reaching its finish line) reads these predicates instead of
growing its own idea of "proven".
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.graph import GraphNode, NodeKind, NodeState
from samurai_ai.domain.kernel import Artifact

#: an artifact the agent wrote or imported itself, with no run behind it
UNBOUND_CREATOR = "agent"

#: a run that actually executed; its output is evidence either way
PROOF_RUN_STATUSES = frozenset({ToolRunStatus.COMPLETED, ToolRunStatus.FAILED})


def artifact_is_bound(artifact: Artifact) -> bool:
    """Harness-produced, operator-supplied, or traced to the run that wrote it."""
    return bool(artifact.source_run_id) or artifact.created_by != UNBOUND_CREATOR


def run_is_proof(run: ToolRun, operation_id: str) -> bool:
    return run.operation_id == operation_id and run.status in PROOF_RUN_STATUSES


def fact_state(*, bound_artifacts: int, proof_runs: int) -> NodeState:
    """A fact is confirmed only by something the harness can trace."""
    return NodeState.CONFIRMED if bound_artifacts or proof_runs else NodeState.CLAIMED


def fact_is_proven(node: GraphNode) -> bool:
    return node.kind is NodeKind.FACT and node.state is NodeState.CONFIRMED


def fact_is_positive(node: GraphNode) -> bool:
    return fact_is_proven(node) and node.payload.get("result", "success") != "negative"


def fact_has_artifacts(node: GraphNode) -> bool:
    return fact_is_proven(node) and bool(node.payload.get("artifact_ids"))


def proven_facts(
    nodes: Iterable[GraphNode], *, since: datetime | None = None
) -> tuple[GraphNode, ...]:
    """Confirmed facts, optionally only those recorded at or after ``since``."""
    return tuple(
        node
        for node in nodes
        if fact_is_proven(node) and (since is None or node.created_at >= since)
    )


__all__ = [
    "PROOF_RUN_STATUSES",
    "UNBOUND_CREATOR",
    "artifact_is_bound",
    "fact_has_artifacts",
    "fact_is_positive",
    "fact_is_proven",
    "fact_state",
    "proven_facts",
    "run_is_proof",
]
