from __future__ import annotations

import pytest
from pydantic import ValidationError

from samurai_ai.domain.redteam import (
    AttackStep,
    AttackStepStateError,
    AttackStepStatus,
    Checkpoint,
    CheckpointStateError,
    CheckpointStatus,
    Debrief,
    DetectionStatus,
    RoeDocument,
    red_team_template,
)


def test_technique_id_validation() -> None:
    step = AttackStep(operation_id="op1", name="cmd exec", technique_id="T1059")
    assert step.technique_id == "T1059"
    sub = AttackStep(operation_id="op1", name="ps", technique_id="T1059.001")
    assert sub.technique_id == "T1059.001"
    for bad in ("1059", "TABC", "T105", "T1059.1", "t1059", "T1059 "):
        with pytest.raises(ValidationError):
            AttackStep(operation_id="op1", name="bad", technique_id=bad)


def test_roe_requires_objectives() -> None:
    with pytest.raises(ValidationError):
        RoeDocument(operation_id="op1", threat_profile="APT29", objectives=())
    roe = RoeDocument(
        operation_id="op1", threat_profile="APT29", objectives=("reach DA",)
    )
    assert "APT29" in roe.describe()
    assert "reach DA" in roe.describe()


def test_attack_step_happy_path() -> None:
    step = AttackStep(operation_id="op1", name="phish", technique_id="T1566")
    assert step.status is AttackStepStatus.PLANNED
    step.begin_execution()
    assert step.status is AttackStepStatus.EXECUTING
    step.complete("payload delivered")
    assert step.status is AttackStepStatus.EXECUTED
    assert step.notes == "payload delivered"
    step.record_detection(DetectionStatus.DETECTED, "EDR alert fired")
    assert step.detection is DetectionStatus.DETECTED
    assert step.version == 4


def test_attack_step_invalid_transitions() -> None:
    step = AttackStep(operation_id="op1", name="dump", technique_id="T1003")
    with pytest.raises(AttackStepStateError):
        step.complete()
    with pytest.raises(AttackStepStateError):
        step.record_detection(DetectionStatus.MISSED)

    step.skip("not applicable")
    assert step.status is AttackStepStatus.SKIPPED
    with pytest.raises(AttackStepStateError):
        step.begin_execution()


def test_attack_step_block_from_executing() -> None:
    step = AttackStep(operation_id="op1", name="lateral", technique_id="T1021")
    step.begin_execution()
    step.block("EDR isolation")
    assert step.status is AttackStepStatus.BLOCKED
    with pytest.raises(AttackStepStateError):
        step.complete()


def test_detection_recorded_once_and_not_pending() -> None:
    step = AttackStep(operation_id="op1", name="exfil", technique_id="T1041")
    step.begin_execution()
    step.complete()
    with pytest.raises(AttackStepStateError):
        step.record_detection(DetectionStatus.PENDING)
    step.record_detection(DetectionStatus.MISSED)
    with pytest.raises(AttackStepStateError):
        step.record_detection(DetectionStatus.DETECTED)


def test_depends_on_deduplicated() -> None:
    step = AttackStep(
        operation_id="op1",
        name="chain",
        technique_id="T1021",
        depends_on=("step_a", "step_a", "step_b"),
    )
    assert step.depends_on == ("step_a", "step_b")


def test_checkpoint_reach_once() -> None:
    checkpoint = Checkpoint(operation_id="op1", name="Before DA")
    assert checkpoint.status is CheckpointStatus.PENDING
    checkpoint.reach("all clear")
    assert checkpoint.status is CheckpointStatus.REACHED
    assert checkpoint.notes == "all clear"
    with pytest.raises(CheckpointStateError):
        checkpoint.reach()


def test_debrief_is_frozen() -> None:
    debrief = Debrief(
        operation_id="op1",
        format="markdown",  # type: ignore[arg-type]
        sha256="a" * 64,
        size_bytes=1,
        path="reports/op1/deb_x.md",
        step_count=2,
    )
    with pytest.raises(ValidationError):
        debrief.step_count = 5  # type: ignore[misc]


def test_red_team_template_structure() -> None:
    template = red_team_template()
    assert template.name == "red-team"
    assert [task.id for task in template.tasks] == [
        "roe-review",
        "path-planning",
        "execution",
        "detection-review",
        "debrief",
    ]
    assert [gate.id for gate in template.gates] == [
        "roe-approved",
        "path-approved",
        "execution-finished",
        "debrief-ready",
    ]
    execution = template.tasks[2]
    assert "attack-path" in execution.required_artifacts
    debrief_gate = template.gates[3]
    assert "final-debrief" in debrief_gate.required_artifacts
