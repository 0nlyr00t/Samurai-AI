"""The finish line and the store decide completion; the judge can only veto."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from samurai_ai.domain.completion import (
    CompletionCriterion,
    CompletionCriterionError,
    CompletionDecision,
    CompletionEvidence,
    completion_criterion,
    decide,
    missing_requirements,
)
from samurai_ai.domain.goal import Goal
from samurai_ai.domain.graph import GraphNode, NodeKind, NodeState
from samurai_ai.domain.kernel import (
    Artifact,
    ArtifactKind,
    Finding,
    FindingSeverity,
    FindingStatus,
)

NOW = datetime.now(UTC)


def _fact(state: NodeState = NodeState.CONFIRMED, *, at: datetime = NOW) -> GraphNode:
    return GraphNode(
        operation_id="op",
        task_id="t",
        kind=NodeKind.FACT,
        state=state,
        payload={"summary": "shell as www-data", "artifact_ids": [], "run_ids": ["run_1"]},
        created_at=at,
    )


def _finding(severity: FindingSeverity, status: FindingStatus = FindingStatus.CANDIDATE) -> Finding:
    return Finding(
        operation_id="op",
        title="RCE",
        severity=severity,
        status=status,
        evidence_ids=("art_1",),
    )


def _artifact(kind: ArtifactKind, created_by: str) -> Artifact:
    return Artifact(
        operation_id="op",
        kind=kind,
        sha256="0" * 64,
        size_bytes=1,
        path="artifacts/op/a.txt",
        created_by=created_by,
    )


def test_the_finish_line_splits_prose_from_requirements() -> None:
    criterion = CompletionCriterion.parse(
        "Read user.txt on the box.\nrequire: finding>=high\nrequire: artifact=file"
    )
    assert criterion.text == "Read user.txt on the box."
    assert criterion.min_finding is FindingSeverity.HIGH
    assert criterion.artifact_kinds == (ArtifactKind.FILE,)


@pytest.mark.parametrize("line", ["require: finding>=huge", "require: artifact=loot", "require: x"])
def test_an_unknown_requirement_fails_loudly(line: str) -> None:
    with pytest.raises(CompletionCriterionError):
        CompletionCriterion.parse(line)


def test_the_finish_line_outranks_objective_and_operation_goal() -> None:
    goal = Goal(operation_id="op", objective="get in", finish_line="root.txt is read")
    assert completion_criterion(goal, "op goal").text == "root.txt is read"
    bare = Goal(operation_id="op", objective="get in")
    assert completion_criterion(bare, "op goal").text == "get in"
    assert completion_criterion(None, "op goal").text == "op goal"
    only_rules = Goal(operation_id="op", objective="get in", finish_line="require: finding>=low")
    assert completion_criterion(only_rules).text == "get in"


def test_no_proof_since_the_goal_began_is_missing() -> None:
    old = _fact(at=NOW - timedelta(hours=1))
    claimed = _fact(NodeState.CLAIMED)
    evidence = CompletionEvidence.collect([old, claimed], [], [], since=NOW - timedelta(minutes=1))
    assert missing_requirements(CompletionCriterion("x"), evidence)

    proven = CompletionEvidence.collect([_fact()], [], [], since=NOW - timedelta(minutes=1))
    assert missing_requirements(CompletionCriterion("x"), proven) == ()


def test_severity_and_artifact_requirements_are_checked() -> None:
    criterion = CompletionCriterion("x", FindingSeverity.HIGH, (ArtifactKind.FILE,))
    weak = CompletionEvidence.collect(
        [_fact()],
        [
            _finding(FindingSeverity.LOW),
            _finding(FindingSeverity.CRITICAL, FindingStatus.DUPLICATE),
        ],
        [_artifact(ArtifactKind.FILE, "agent")],
    )
    assert len(missing_requirements(criterion, weak)) == 2

    strong = CompletionEvidence.collect(
        [], [_finding(FindingSeverity.CRITICAL)], [_artifact(ArtifactKind.FILE, "tool:x")]
    )
    assert missing_requirements(criterion, strong) == ()


def test_evidence_can_veto_the_judge_but_never_replace_it() -> None:
    assert decide("stop", ()) is CompletionDecision.COMPLETE
    assert decide("stop", ("no proof",)) is CompletionDecision.UNPROVEN
    assert decide("continue", ()) is CompletionDecision.CONTINUE
    assert decide("unclear", ()) is CompletionDecision.CONTINUE
    assert decide(None, ()) is CompletionDecision.UNAVAILABLE
