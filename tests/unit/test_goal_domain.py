from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from samurai_ai.domain.goal import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_BUDGET_STEPS,
    Goal,
    GoalState,
    GoalStateError,
)


def make_goal(**overrides: object) -> Goal:
    payload: dict[str, object] = {"operation_id": "op_1", "objective": "Get admin on X"}
    payload.update(overrides)
    return Goal.model_validate(payload)


def test_goal_defaults_and_budget_maths() -> None:
    goal = make_goal()
    assert goal.state is GoalState.ACTIVE
    assert goal.block_reason == ""
    assert goal.budget_steps == DEFAULT_BUDGET_STEPS
    assert goal.budget_seconds == DEFAULT_BUDGET_SECONDS
    assert goal.steps_left == DEFAULT_BUDGET_STEPS
    assert goal.exhausted is False

    goal.record_step(3)
    assert goal.steps_left == DEFAULT_BUDGET_STEPS - 3
    goal.tick(10)
    assert goal.seconds_left == DEFAULT_BUDGET_SECONDS - 10


def test_budget_exhaustion_is_reported() -> None:
    steps = make_goal(budget_steps=2)
    steps.record_step(2)
    assert steps.exhausted is True

    seconds = make_goal(budget_seconds=5)
    seconds.tick(5)
    assert seconds.exhausted is True


def test_extend_budget_raises_only_the_spent_limits() -> None:
    goal = make_goal(budget_steps=2, budget_seconds=1800)
    goal.record_step(2)
    goal.extend_budget()
    assert goal.budget_steps == 2 + DEFAULT_BUDGET_STEPS
    assert goal.budget_seconds == 1800
    assert goal.exhausted is False

    timed = make_goal(budget_seconds=5)
    timed.tick(5)
    timed.extend_budget()
    assert timed.budget_seconds == 5 + DEFAULT_BUDGET_SECONDS
    assert timed.budget_steps == DEFAULT_BUDGET_STEPS


def test_tick_ignores_zero_and_rejects_negative() -> None:
    goal = make_goal()
    version = goal.version
    goal.tick(0)
    assert goal.version == version
    with pytest.raises(ValueError, match="negative"):
        goal.tick(-1)


def test_state_machine_happy_path() -> None:
    goal = make_goal()
    goal.pause()
    assert goal.state is GoalState.PAUSED
    goal.resume()
    goal.block("no credentials")
    assert goal.block_reason == "no credentials"
    goal.resume()
    assert goal.block_reason == ""
    goal.complete()
    assert goal.state is GoalState.COMPLETE
    assert goal.state.terminal


def test_terminal_states_are_final() -> None:
    for finish in (GoalState.COMPLETE, GoalState.CANCELLED):
        goal = make_goal()
        if finish is GoalState.COMPLETE:
            goal.complete()
        else:
            goal.cancel()
        with pytest.raises(GoalStateError):
            goal.pause()
        with pytest.raises(GoalStateError):
            goal.cancel()


def test_queued_goal_promotes_to_active() -> None:
    queued = make_goal(state=GoalState.QUEUED)
    with pytest.raises(GoalStateError):
        queued.pause()
    queued.promote()
    assert queued.state is GoalState.ACTIVE


def test_block_requires_and_bounds_a_reason() -> None:
    goal = make_goal()
    with pytest.raises(ValueError, match="reason"):
        goal.block("  ")
    goal.block("x" * 5000)
    assert len(goal.block_reason) == 4000


def test_blocked_flag_and_reason_stay_consistent() -> None:
    goal = make_goal()
    goal.block("no credentials")
    # leaving the blocked state clears the reason again
    goal.complete()
    assert goal.block_reason == ""
    assert goal.state is GoalState.COMPLETE

    other = make_goal()
    other.block("x")
    other.resume()
    assert other.block_reason == ""

    with pytest.raises(GoalStateError, match="already blocked"):
        other.block("y")
        other.block("z")


def test_objective_is_required_and_bounded() -> None:
    with pytest.raises(ValidationError):
        make_goal(objective="")
    with pytest.raises(ValidationError):
        make_goal(objective="x" * 4001)


def test_mutations_bump_version_and_timestamp() -> None:
    goal = make_goal()
    goal.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    version = goal.version
    goal.pause()
    assert goal.version == version + 1
    assert goal.updated_at.year > 2020
