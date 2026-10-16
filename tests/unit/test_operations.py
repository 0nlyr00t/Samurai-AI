import pytest

from samurai_ai.domain.operations import (
    Operation,
    OperationMode,
    OperationStatus,
    OperationTransitionError,
)
from samurai_ai.domain.scope import ScopeContract


def make_operation() -> Operation:
    return Operation(
        name="  Authorized lab  ",
        mode=OperationMode.CTF_HTB,
        scope=ScopeContract(includes=("192.0.2.10",)),
        objective="Solve the lab and preserve a replay",
    )


def test_operation_follows_the_lifecycle() -> None:
    operation = make_operation()
    assert operation.name == "Authorized lab"
    assert operation.status == OperationStatus.DRAFT
    assert operation.version == 1

    previous = operation.transition_to(OperationStatus.PLANNING)
    assert previous == OperationStatus.DRAFT
    assert operation.status == OperationStatus.PLANNING
    assert operation.version == 2

    operation.transition_to(OperationStatus.APPROVAL)
    operation.transition_to(OperationStatus.EXECUTION)
    operation.transition_to(OperationStatus.VERIFICATION)
    operation.transition_to(OperationStatus.REPORTING)
    operation.transition_to(OperationStatus.COMPLETE)
    assert operation.status == OperationStatus.COMPLETE


def test_invalid_transition_is_rejected_without_mutation() -> None:
    operation = make_operation()

    with pytest.raises(OperationTransitionError):
        operation.transition_to(OperationStatus.EXECUTION)

    assert operation.status == OperationStatus.DRAFT
    assert operation.version == 1


def test_completed_operation_is_terminal() -> None:
    operation = make_operation()
    operation.status = OperationStatus.COMPLETE

    with pytest.raises(OperationTransitionError):
        operation.transition_to(OperationStatus.PLANNING)


def test_paused_operation_can_resume_to_an_active_stage() -> None:
    operation = make_operation()
    operation.transition_to(OperationStatus.PLANNING)
    operation.transition_to(OperationStatus.PAUSED)

    operation.transition_to(OperationStatus.PLANNING)
    assert operation.status == OperationStatus.PLANNING
