import json

import pytest

from samurai_ai.domain.goal import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_BUDGET_STEPS,
    GoalError,
    GoalState,
)
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.goals import GoalNotFoundError, GoalService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.goals import GoalConflictError, GoalRepository
from samurai_ai.storage.operations import OperationRepository


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_service(tmp_path) -> tuple[GoalService, GoalRepository, OperationService, Operation]:  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "goals.db")
    operations = OperationService(OperationRepository(database))
    clock = FakeClock()
    goal_service = GoalService(operations, GoalRepository(database), clock=clock)
    operation = operations.create(
        "Goal op",
        OperationMode.RED_TEAM,
        ScopeContract(includes=("example.com",)),
    )
    goal_service.clock = clock  # type: ignore[assignment]
    return goal_service, GoalRepository(database), operations, operation


def test_create_and_read_a_goal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "Get DA on X", finish_line="da.txt exists")

    assert goal.state is GoalState.ACTIVE
    assert service.current(operation.id) == goal
    assert service.get(goal.id).finish_line == "da.txt exists"

    events = [
        event.event_type
        for event in _operations.events(operation.id)
        if event.event_type.startswith("goal.")
    ]
    assert events == ["goal.created"]


def test_second_goal_is_queued_and_promoted_on_complete(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    first = service.create(operation.id, "first")
    second = service.create(operation.id, "second")

    assert second.state is GoalState.QUEUED
    assert service.current(operation.id) == first

    service.complete(first.id)
    promoted = service.get(second.id)
    assert promoted.state is GoalState.ACTIVE


def test_goal_lineage_preserves_the_original_evidence_window(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    root = service.create(operation.id, "research the documentation")
    service.complete(root.id)

    child = service.create(
        operation.id,
        root.objective,
        parent_goal_id=root.id,
    )

    assert child.parent_goal_id == root.id
    assert service.evidence_since(child) == root.created_at
    independent = service.create(operation.id, "a different objective")
    assert independent.parent_goal_id == ""
    assert service.evidence_since(independent) == independent.created_at
    other = _operations.create(
        "Another operation", OperationMode.RED_TEAM, ScopeContract(includes=("example.com",))
    )
    with pytest.raises(GoalError, match="same operation"):
        service.create(other.id, "borrow proof", parent_goal_id=root.id)


def test_block_metadata_survives_restart_and_clears_on_resume(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "probe")

    blocked = service.block(
        goal.id,
        "finalization_required",
        code="budget_exhausted",
        recovery="/goal extend 40 1800",
    )
    assert blocked.block_code == "budget_exhausted"
    assert blocked.recovery == "/goal extend 40 1800"
    assert service.get(goal.id).block_code == "budget_exhausted"

    resumed = service.resume(goal.id)
    assert resumed.block_reason == ""
    assert resumed.block_code == ""
    assert resumed.recovery == ""


def test_clock_only_advances_while_active(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "probe", budget_seconds=100)

    service.clock.advance(10)  # type: ignore[attr-defined]
    paused = service.pause(goal.id)
    assert paused.spent_seconds == 10

    service.clock.advance(50)  # type: ignore[attr-defined]
    assert service.get(goal.id).spent_seconds == 10  # paused: no accrual

    service.resume(goal.id)
    service.clock.advance(5)  # type: ignore[attr-defined]
    # time is accrued at the next transition, not on read
    assert service.get(goal.id).spent_seconds == 10
    service.record_step(goal.id)
    assert service.get(goal.id).spent_seconds == 15


def test_step_budget_enters_finalizing_until_operator_extends(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "short", budget_steps=2)

    service.record_step(goal.id)
    assert service.get(goal.id).state is GoalState.ACTIVE
    exhausted = service.record_step(goal.id)

    assert exhausted.state is GoalState.FINALIZING
    assert exhausted.budget_steps == 2
    assert exhausted.spent_steps == 2
    assert service.record_step(goal.id).state is GoalState.FINALIZING

    extended = service.extend(
        goal.id, steps=DEFAULT_BUDGET_STEPS, seconds=DEFAULT_BUDGET_SECONDS
    )
    assert extended.state is GoalState.ACTIVE
    assert extended.budget_steps == 2 + DEFAULT_BUDGET_STEPS
    assert service.record_step(goal.id).state is GoalState.ACTIVE


def test_time_budget_enters_finalizing_until_operator_extends(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "slow", budget_seconds=30)

    service.clock.advance(30)  # type: ignore[attr-defined]
    exhausted = service.record_step(goal.id)

    assert exhausted.state is GoalState.FINALIZING
    assert exhausted.budget_seconds == 30
    assert exhausted.spent_seconds == 30

    extended = service.extend(goal.id, steps=1, seconds=DEFAULT_BUDGET_SECONDS)
    assert extended.state is GoalState.ACTIVE
    assert extended.budget_seconds == 30 + DEFAULT_BUDGET_SECONDS


def test_replace_cancels_the_current_goal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    first = service.create(operation.id, "first")
    replacement = service.replace(operation.id, "second")

    assert service.get(first.id).state is GoalState.CANCELLED
    assert replacement.state is GoalState.ACTIVE


def test_completion_promotes_the_queue_only_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    first = service.create(operation.id, "first")
    queued = service.create(operation.id, "second")
    service.cancel(first.id)

    assert service.get(queued.id).state is GoalState.ACTIVE
    assert service.get(first.id).state is GoalState.CANCELLED


def test_unknown_goal_and_operation_are_reported(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, _operation = make_service(tmp_path)
    with pytest.raises(GoalNotFoundError):
        service.get("goal_missing")
    with pytest.raises(ValueError, match="operation not found"):
        service.create("op_missing", "x")


def test_repository_detects_a_stale_version(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "race")

    stale = repo.get(goal.id)
    assert stale is not None
    service.block(goal.id, "first writer")

    stale.pause()
    from samurai_ai.domain.operations import OperationEvent

    with pytest.raises(GoalConflictError):
        repo.save(
            stale,
            goal.version,
            OperationEvent(
                operation_id=operation.id, event_type="goal.paused", payload={}
            ),
        )


def test_events_carry_the_state_and_goal_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "audit me")
    service.pause(goal.id)

    paused = [
        json.loads(event.payload) if isinstance(event.payload, str) else event.payload
        for event in operations.events(operation.id)
        if event.event_type == "goal.paused"
    ]
    assert paused[0]["goal_id"] == goal.id
    assert paused[0]["state"] == "paused"


def test_sub_second_steps_still_spend_the_clock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Frequent steps used to drop every fraction: 0.6 s × 5 counted as 0 s."""
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "quick steps")
    for _ in range(5):
        service.clock.advance(0.6)  # type: ignore[attr-defined]
        goal = service.record_step(goal.id)
    assert goal.spent_seconds == 3


def test_each_goal_keeps_its_own_clock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, operations, operation = make_service(tmp_path)
    other_op = operations.create(
        "Other op", OperationMode.RED_TEAM, ScopeContract(includes=("example.org",))
    )
    first = service.create(operation.id, "first")
    service.clock.advance(10)  # type: ignore[attr-defined]
    second = service.create(other_op.id, "second")  # must not reset the first clock
    service.clock.advance(5)  # type: ignore[attr-defined]
    assert service.record_step(first.id).spent_seconds == 15
    assert service.record_step(second.id).spent_seconds == 5
