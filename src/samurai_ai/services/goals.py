"""Goal service: operator-owned objectives with a verifiable finish line.

The budget is enforced here, not in the model: a goal blocks itself when it runs
out of steps or wall-clock time, and the block reason says which limit hit. The
clock only advances while a goal is active, so pausing stops it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from samurai_ai.domain.goal import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_BUDGET_STEPS,
    Goal,
    GoalError,
    GoalState,
)
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.goals import GoalRepository


class GoalNotFoundError(LookupError):
    pass


class GoalService:
    def __init__(
        self,
        operations: OperationService,
        goals: GoalRepository,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.operations = operations
        self.goals = goals
        self.clock = clock
        self._last_tick: float | None = None

    # --- creation ---------------------------------------------------------

    def create(
        self,
        operation_id: str,
        objective: str,
        *,
        finish_line: str = "",
        budget_steps: int = DEFAULT_BUDGET_STEPS,
        budget_seconds: int = DEFAULT_BUDGET_SECONDS,
        queue: bool = False,
    ) -> Goal:
        """Create a goal; ``queue`` puts it behind the current one."""
        self._require_operation(operation_id)
        state = GoalState.QUEUED if queue or self.goals.current(operation_id) else GoalState.ACTIVE
        goal = Goal(
            operation_id=operation_id,
            objective=objective,
            finish_line=finish_line,
            budget_steps=budget_steps,
            budget_seconds=budget_seconds,
            state=state,
        )
        self.goals.add(
            goal,
            self._event(goal, "goal.created", {"objective": goal.objective}),
        )
        if goal.state is GoalState.ACTIVE:
            self.restart_clock()
        return goal

    def replace(self, operation_id: str, objective: str, **kwargs: object) -> Goal:
        current = self.goals.current(operation_id)
        if current is not None:
            self.cancel(current.id)
        return self.create(operation_id, objective, **kwargs)  # type: ignore[arg-type]

    # --- reads ------------------------------------------------------------

    def get(self, goal_id: str) -> Goal:
        goal = self.goals.get(goal_id)
        if goal is None:
            raise GoalNotFoundError(f"goal not found: {goal_id}")
        return goal

    def current(self, operation_id: str) -> Goal | None:
        return self.goals.current(operation_id)

    def list(self, operation_id: str) -> list[Goal]:
        return self.goals.list(operation_id)

    # --- transitions ------------------------------------------------------

    def pause(self, goal_id: str) -> Goal:
        goal = self.get(goal_id)
        expected = goal.version
        self._accrue(goal)
        goal.pause()
        return self._save(goal, expected, "goal.paused", {})

    def resume(self, goal_id: str) -> Goal:
        goal = self.get(goal_id)
        expected = goal.version
        goal.resume()
        saved = self._save(goal, expected, "goal.resumed", {})
        self.restart_clock()
        return saved

    def block(self, goal_id: str, reason: str) -> Goal:
        goal = self.get(goal_id)
        expected = goal.version
        self._accrue(goal)
        goal.block(reason)
        return self._save(goal, expected, "goal.blocked", {"reason": goal.block_reason})

    def complete(self, goal_id: str) -> Goal:
        goal = self.get(goal_id)
        expected = goal.version
        self._accrue(goal)
        goal.complete()
        saved = self._save(goal, expected, "goal.complete", {})
        self._stop_clock()
        self._promote_next(goal.operation_id)
        return saved

    def cancel(self, goal_id: str) -> Goal:
        goal = self.get(goal_id)
        expected = goal.version
        self._accrue(goal)
        goal.cancel()
        saved = self._save(goal, expected, "goal.cancelled", {})
        self._stop_clock()
        self._promote_next(goal.operation_id)
        return saved

    # --- budget -----------------------------------------------------------

    def record_step(self, goal_id: str) -> Goal:
        """Count one agent step against the active goal and enforce the budget."""
        goal = self.get(goal_id)
        if goal.state is not GoalState.ACTIVE:
            return goal
        expected = goal.version
        self._accrue(goal)
        goal.record_step()
        if goal.exhausted:
            goal.block(self._exhaustion_reason(goal))
            return self._save(goal, expected, "goal.blocked", {"reason": goal.block_reason})
        return self._save(goal, expected, "goal.step", {"spent_steps": goal.spent_steps})

    def restart_clock(self) -> None:
        self._last_tick = self.clock()

    def _stop_clock(self) -> None:
        self._last_tick = None

    def _accrue(self, goal: Goal) -> None:
        """Move wall-clock time into the goal while it is active."""
        if goal.state is not GoalState.ACTIVE or self._last_tick is None:
            return
        elapsed = int(self.clock() - self._last_tick)
        self._last_tick = self.clock()
        if elapsed > 0:
            goal.tick(elapsed)

    @staticmethod
    def _exhaustion_reason(goal: Goal) -> str:
        limits = []
        if goal.spent_steps >= goal.budget_steps:
            limits.append(f"step budget {goal.budget_steps} spent")
        if goal.spent_seconds >= goal.budget_seconds:
            limits.append(f"time budget {goal.budget_seconds}s spent")
        return "budget exhausted: " + ", ".join(limits)

    # --- internals --------------------------------------------------------

    def _promote_next(self, operation_id: str) -> None:
        queued = self.goals.next_queued(operation_id)
        if queued is None:
            return
        expected = queued.version
        queued.promote()
        self._save(queued, expected, "goal.promoted", {})
        self.restart_clock()

    def _require_operation(self, operation_id: str) -> None:
        if self.operations.repository.get(operation_id) is None:
            raise GoalError(f"operation not found: {operation_id}")

    def _save(
        self,
        goal: Goal,
        expected_version: int,
        event_type: str,
        payload: dict[str, object],
    ) -> Goal:
        self.goals.save(
            goal, expected_version, self._event(goal, event_type, payload)
        )
        return goal

    @staticmethod
    def _event(goal: Goal, event_type: str, payload: dict[str, object]) -> OperationEvent:
        return OperationEvent(
            operation_id=goal.operation_id,
            event_type=event_type,
            payload={"goal_id": goal.id, "state": goal.state.value, **payload},
        )
