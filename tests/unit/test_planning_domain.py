from __future__ import annotations

import pytest
from pydantic import ValidationError

from samurai_ai.domain.planning import (
    KillChainPhase,
    PlannedTest,
    PlannedTestStateError,
    PlannedTestStatus,
    PlanScenario,
    Runbook,
    planning_template,
)


def test_kill_chain_covers_seven_phases() -> None:
    assert {phase.value for phase in KillChainPhase} == {
        "reconnaissance",
        "weaponization",
        "delivery",
        "exploitation",
        "installation",
        "command-and-control",
        "actions-on-objectives",
    }


def test_technique_id_validation() -> None:
    case = PlannedTest(
        operation_id="op1",
        name="phish",
        technique_id="T1566.001",
        kill_chain_phase=KillChainPhase.DELIVERY,
    )
    assert case.technique_id == "T1566.001"
    for bad in ("1566", "T15661", "T1566.0001", "X1566"):
        with pytest.raises(ValidationError):
            PlannedTest(
                operation_id="op1",
                name="bad",
                technique_id=bad,
                kill_chain_phase=KillChainPhase.DELIVERY,
            )


def test_scenario_describe() -> None:
    scenario = PlanScenario(
        operation_id="op1",
        name="Q3 emulation",
        description="finance segment",
        schedule="weekdays 09:00-12:00",
    )
    text = scenario.describe()
    assert "Q3 emulation" in text
    assert "weekdays 09:00-12:00" in text


def test_test_case_state_machine() -> None:
    case = PlannedTest(
        operation_id="op1",
        name="c2 beacon",
        technique_id="T1071.001",
        kill_chain_phase=KillChainPhase.COMMAND_AND_CONTROL,
    )
    assert case.status is PlannedTestStatus.DRAFT
    case.mark_ready("mapping verified")
    assert case.status is PlannedTestStatus.READY
    assert case.notes == "mapping verified"
    with pytest.raises(PlannedTestStateError):
        case.mark_ready()
    case.retire("replaced by atomic test")
    assert case.status is PlannedTestStatus.RETIRED
    with pytest.raises(PlannedTestStateError):
        case.retire()


def test_depends_on_deduplicated() -> None:
    case = PlannedTest(
        operation_id="op1",
        name="chain",
        technique_id="T1021",
        kill_chain_phase=KillChainPhase.EXPLOITATION,
        depends_on=("tc_a", "tc_a", "tc_b"),
    )
    assert case.depends_on == ("tc_a", "tc_b")


def test_runbook_is_frozen() -> None:
    runbook = Runbook(
        operation_id="op1",
        format="markdown",  # type: ignore[arg-type]
        sha256="a" * 64,
        size_bytes=1,
        path="reports/op1/rb_x.md",
        test_case_ids=("tc_1",),
        stop_points=1,
    )
    with pytest.raises(ValidationError):
        runbook.stop_points = 3  # type: ignore[misc]


def test_planning_template_structure() -> None:
    template = planning_template()
    assert template.name == "operation-plan"
    assert [task.id for task in template.tasks] == [
        "scenario-draft",
        "mapping",
        "test-design",
        "review",
        "runbook",
    ]
    assert [gate.id for gate in template.gates] == [
        "scenario-locked",
        "mapping-complete",
        "tests-ready",
        "runbook-ready",
    ]
    runbook_gate = template.gates[3]
    assert "final-runbook" in runbook_gate.required_artifacts
