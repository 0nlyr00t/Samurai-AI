"""Constraints: validated, capped, and rendered deny-first."""

from __future__ import annotations

from pathlib import Path

import pytest

from samurai_ai.domain.constraints import (
    MAX_CONSTRAINTS,
    TEXT_MAX,
    ConstraintError,
    ConstraintKind,
)
from samurai_ai.domain.kernel import PlanTask
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.constraints import ConstraintNotFoundError, ConstraintService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.constraints import ConstraintRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository


@pytest.fixture
def stack(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "constraints.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    constraints = ConstraintService(operations, ConstraintRepository(database))
    operation = operations.create("gate", OperationMode.BUG_BOUNTY, ScopeContract(includes=("x",)))
    plan = kernel.create_plan(operation.id, "plan", [PlanTask(id="recon", title="Recon")], [])
    return constraints, operations, operation.id, str(plan.tasks[0].id)


def test_a_deny_constraint_is_stored_and_journalled(stack) -> None:
    constraints, operations, operation_id, task_id = stack

    constraint = constraints.add(
        operation_id, task_id, ConstraintKind.DENY, "  do not touch /admin "
    )

    assert constraint.text == "do not touch /admin"
    types = [event.event_type for event in operations.repository.events(operation_id)]
    assert "constraint.added" in types


def test_empty_text_is_refused(stack) -> None:
    constraints, _operations, operation_id, task_id = stack

    with pytest.raises(ConstraintError):
        constraints.add(operation_id, task_id, ConstraintKind.ALLOW, "   ")


def test_long_text_is_refused(stack) -> None:
    constraints, _operations, operation_id, task_id = stack

    with pytest.raises(ConstraintError):
        constraints.add(operation_id, task_id, ConstraintKind.ALLOW, "x" * (TEXT_MAX + 1))


def test_the_cap_is_enforced(stack) -> None:
    constraints, _operations, operation_id, task_id = stack
    for index in range(MAX_CONSTRAINTS):
        constraints.add(operation_id, task_id, ConstraintKind.ALLOW, f"area {index}")

    with pytest.raises(ConstraintError):
        constraints.add(operation_id, task_id, ConstraintKind.DENY, "one too many")


def test_the_block_puts_denies_first(stack) -> None:
    constraints, _operations, operation_id, task_id = stack
    constraints.add(operation_id, task_id, ConstraintKind.ALLOW, "only /api/v2")
    constraints.add(operation_id, task_id, ConstraintKind.DENY, "do not touch /admin")

    block = constraints.block(task_id)

    assert block.index("must not") < block.index("only")
    assert "- do not touch /admin" in block


def test_an_empty_task_has_no_block(stack) -> None:
    constraints, _operations, _operation_id, task_id = stack

    assert constraints.block(task_id) == ""


def test_removal_is_journalled_and_idempotent_enough(stack) -> None:
    constraints, operations, operation_id, task_id = stack
    constraint = constraints.add(operation_id, task_id, ConstraintKind.DENY, "no /admin")

    constraints.remove(constraint.id)

    assert constraints.for_task(task_id) == []
    assert "constraint.removed" in [
        event.event_type for event in operations.repository.events(operation_id)
    ]
    with pytest.raises(ConstraintNotFoundError):
        constraints.remove(constraint.id)
