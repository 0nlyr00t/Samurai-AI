import json

import pytest

from samurai_ai.domain.goal import GoalState
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


def test_step_budget_blocks_the_goal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "short", budget_steps=2)

    service.record_step(goal.id)
    assert service.get(goal.id).state is GoalState.ACTIVE
    blocked = service.record_step(goal.id)

    assert blocked.state is GoalState.BLOCKED
    assert "step budget 2 spent" in blocked.block_reason
    # a blocked goal records no further steps
    assert service.record_step(goal.id).state is GoalState.BLOCKED


def test_time_budget_blocks_the_goal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service, _repo, _operations, operation = make_service(tmp_path)
    goal = service.create(operation.id, "slow", budget_seconds=30)

    service.clock.advance(30)  # type: ignore[attr-defined]
    blocked = service.record_step(goal.id)

    assert blocked.state is GoalState.BLOCKED
    assert "time budget 30s spent" in blocked.block_reason


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
