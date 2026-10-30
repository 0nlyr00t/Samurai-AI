"""Goal mode: an objective with a verifiable finish line and a budget.

A goal answers "what must become true", not "what to do next". The budget ticks
only while the goal is active and the session is open, so a paused goal does not
drain it. Exhaustion blocks the goal with a reason instead of silently stopping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_OBJECTIVE = 4000
DEFAULT_BUDGET_STEPS = 40
DEFAULT_BUDGET_SECONDS = 1800


class GoalError(ValueError):
    pass


class GoalStateError(GoalError):
    pass


class GoalState(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (GoalState.COMPLETE, GoalState.CANCELLED)


_TRANSITIONS: dict[GoalState, frozenset[GoalState]] = {
    GoalState.QUEUED: frozenset({GoalState.ACTIVE, GoalState.CANCELLED}),
    GoalState.ACTIVE: frozenset(
        {
            GoalState.PAUSED,
            GoalState.BLOCKED,
            GoalState.COMPLETE,
            GoalState.CANCELLED,
        }
    ),
    GoalState.PAUSED: frozenset({GoalState.ACTIVE, GoalState.CANCELLED}),
    GoalState.BLOCKED: frozenset(
        {GoalState.ACTIVE, GoalState.COMPLETE, GoalState.CANCELLED}
    ),
    GoalState.COMPLETE: frozenset(),
    GoalState.CANCELLED: frozenset(),
}


class Goal(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"goal_{uuid4().hex[:12]}")
    operation_id: str
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE)
    finish_line: str = Field(default="", max_length=MAX_OBJECTIVE)
    budget_steps: int = Field(default=DEFAULT_BUDGET_STEPS, ge=1)
    budget_seconds: int = Field(default=DEFAULT_BUDGET_SECONDS, ge=1)
    spent_steps: int = Field(default=0, ge=0)
    spent_seconds: int = Field(default=0, ge=0)
    state: GoalState = GoalState.ACTIVE
    block_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = Field(default=1, ge=1)

    # --- budget -----------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return (
            self.spent_steps >= self.budget_steps
            or self.spent_seconds >= self.budget_seconds
        )

    @property
    def steps_left(self) -> int:
        return max(0, self.budget_steps - self.spent_steps)

    @property
    def seconds_left(self) -> int:
        return max(0, self.budget_seconds - self.spent_seconds)

    def record_step(self, steps: int = 1) -> None:
        if steps < 1:
            raise GoalError("a step must be at least 1")
        self.spent_steps += steps
        self.touch()

    def tick(self, seconds: int) -> None:
        if seconds < 0:
            raise GoalError("elapsed time cannot be negative")
        if seconds == 0:
            return
        self.spent_seconds += seconds
        self.touch()

    # --- state machine ----------------------------------------------------

    def _move(self, target: GoalState) -> None:
        if target not in _TRANSITIONS[self.state]:
            raise GoalStateError(f"cannot move goal from {self.state} to {target}")
        if self.state is GoalState.BLOCKED:
            # the model validator rejects a stray reason on a non-blocked goal,
            # and assignments validate, so clear it before changing the state
            self.block_reason = ""
        self.state = target

    def pause(self) -> None:
        self._move(GoalState.PAUSED)
        self.touch()

    def resume(self) -> None:
        self._move(GoalState.ACTIVE)
        self.block_reason = ""
        self.touch()

    def block(self, reason: str) -> None:
        if not reason.strip():
            raise GoalError("blocking a goal needs a reason")
        if self.state is GoalState.BLOCKED:
            raise GoalStateError("goal is already blocked")
        if GoalState.BLOCKED not in _TRANSITIONS[self.state]:
            raise GoalStateError(f"cannot block goal in state {self.state}")
        self.block_reason = reason.strip()[:MAX_OBJECTIVE]
        self.state = GoalState.BLOCKED
        self.touch()

    def complete(self) -> None:
        self._move(GoalState.COMPLETE)
        self.touch()

    def cancel(self) -> None:
        self._move(GoalState.CANCELLED)
        self.touch()

    def promote(self) -> None:
        """Turn a queued goal into the active one."""
        self._move(GoalState.ACTIVE)
        self.touch()

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
        self.version += 1
