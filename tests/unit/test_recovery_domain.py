import pytest

from samurai_ai.domain.kernel import OperationPlan, PlanTask, RiskClass, StageGate
from samurai_ai.domain.recovery import (
    RecoveryAction,
    RecoveryActionStatus,
    RecoveryStateError,
    diff_plans,
    diff_scopes,
)
from samurai_ai.domain.scope import ScopeContract


def make_action() -> RecoveryAction:
    return RecoveryAction(
        operation_id="op_test",
        action_type="tool-run",
        description="Run the scanner",
        task_id="recon",
    )


def test_recovery_action_lifecycle_transitions() -> None:
    action = make_action()
    assert action.status == RecoveryActionStatus.IN_PROGRESS
    assert action.version == 1

    action.complete()
    assert action.status == RecoveryActionStatus.COMPLETED
    assert action.version == 2
    assert action.updated_at >= action.created_at

    with pytest.raises(RecoveryStateError):
        action.complete()
    with pytest.raises(RecoveryStateError):
        action.fail()
    with pytest.raises(RecoveryStateError):
        action.mark_unknown()


def test_recovery_action_fail_and_mark_unknown() -> None:
    failed = make_action()
    failed.fail()
    assert failed.status == RecoveryActionStatus.FAILED
    assert failed.version == 2
    with pytest.raises(RecoveryStateError):
        failed.mark_unknown()

    unknown = make_action()
    unknown.mark_unknown()
    assert unknown.status == RecoveryActionStatus.UNKNOWN
    assert unknown.version == 2
    with pytest.raises(RecoveryStateError):
        unknown.complete()


def make_plan(operation_id: str, tasks: list[PlanTask], gates: list[StageGate]) -> OperationPlan:
    return OperationPlan(operation_id=operation_id, title="Plan", tasks=tasks, gates=gates)


def test_diff_plans_detects_added_removed_changed() -> None:
    old = make_plan(
        "op_test",
        [
            PlanTask(id="recon", title="Map services"),
            PlanTask(id="enum", title="Enumerate", depends_on=("recon",)),
        ],
        [StageGate(id="recon-done", name="Recon done")],
    )
    new = make_plan(
        "op_test",
        [
            PlanTask(id="recon", title="Map services", risk_class=RiskClass.R2),
            PlanTask(id="exploit", title="Exploit", depends_on=("recon",)),
        ],
        [
            StageGate(id="recon-done", name="Recon done"),
            StageGate(id="exploit-done", name="Exploit done"),
        ],
    )

    diff = diff_plans(old, new)
    assert diff.tasks.added == ("exploit",)
    assert diff.tasks.removed == ("enum",)
    assert diff.tasks.changed == ("recon",)
    assert diff.gates.added == ("exploit-done",)
    assert diff.gates.removed == ()
    assert diff.gates.changed == ()

    identical = diff_plans(old, old)
    assert identical.tasks.added == ()
    assert identical.tasks.removed == ()
    assert identical.tasks.changed == ()
    assert identical.gates.changed == ()


def test_diff_scopes_has_no_changed_entries() -> None:
    old = ScopeContract(
        includes=("*.example.com", "192.0.2.0/24"),
        excludes=("api.example.com",),
    )
    new = ScopeContract(
        includes=("*.example.com", "198.51.100.10"),
        excludes=("api.example.com", "www.example.com"),
    )

    diff = diff_scopes(old, new)
    assert diff.includes.added == ("198.51.100.10",)
    assert diff.includes.removed == ("192.0.2.0/24",)
    assert diff.includes.changed == ()
    assert diff.excludes.added == ("www.example.com",)
    assert diff.excludes.removed == ()
    assert diff.excludes.changed == ()

    identical = diff_scopes(old, old)
    assert identical.includes.added == ()
    assert identical.includes.removed == ()
    assert identical.excludes.added == ()
    assert identical.excludes.removed == ()
