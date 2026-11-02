"""Impact ladder: how far a confirmed finding was actually pushed.

A finding that reproduces is worth little on its own. The ladder names the
levels an operator may escalate through, keeps a bounded budget for it, and
grades the result — so "validated" stops being the finish line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

MAX_ATTEMPTS = 8
MAX_REQUESTS = 120
MAX_SECONDS = 3600.0
REPORTABLE_POINTS = 3


class ImpactError(ValueError):
    pass


class ImpactBudgetError(ImpactError):
    pass


class ImpactLevel(StrEnum):
    """Ordered by what an attacker gains, cheapest first."""

    READ = "read"
    WRITE = "write"
    AUTH_BYPASS = "auth-bypass"
    DATA_EXPORT = "data-export"
    RCE = "rce"

    @property
    def points(self) -> int:
        return _POINTS[self]

    @property
    def state_changing(self) -> bool:
        return self is not ImpactLevel.READ


_POINTS = {
    ImpactLevel.READ: 1,
    ImpactLevel.WRITE: 2,
    ImpactLevel.AUTH_BYPASS: 3,
    ImpactLevel.DATA_EXPORT: 4,
    ImpactLevel.RCE: 5,
}

_ORDER = tuple(ImpactLevel)


@dataclass
class ImpactBudget:
    """What one escalation session may spend; refusals are explicit."""

    max_attempts: int = MAX_ATTEMPTS
    max_requests: int = MAX_REQUESTS
    max_seconds: float = MAX_SECONDS
    attempts: int = 0
    requests: int = 0
    seconds: float = 0.0

    def spend(self, requests: int = 1, seconds: float = 0.0) -> None:
        if self.attempts >= self.max_attempts:
            raise ImpactBudgetError(
                f"escalation budget is spent: {self.attempts} attempts"
            )
        if self.requests + requests > self.max_requests:
            raise ImpactBudgetError(
                f"escalation budget is spent: {self.requests} requests"
            )
        if self.seconds + seconds > self.max_seconds:
            raise ImpactBudgetError(
                f"escalation budget is spent: {self.seconds:.0f}s"
            )
        self.attempts += 1
        self.requests += requests
        self.seconds += seconds

    @property
    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.attempts)


@dataclass(frozen=True)
class ImpactAttempt:
    finding_id: str
    level: ImpactLevel
    evidence: tuple[str, ...]
    outcome: str = "confirmed"
    #: how to undo what the attempt changed (required for state changes)
    rollback: str = ""


@dataclass
class ImpactState:
    finding_id: str
    attempts: list[ImpactAttempt] = field(default_factory=list)
    budget: ImpactBudget = field(default_factory=ImpactBudget)

    @property
    def confirmed(self) -> list[ImpactAttempt]:
        return [item for item in self.attempts if item.outcome == "confirmed"]

    @property
    def level(self) -> ImpactLevel | None:
        """The highest level actually confirmed."""
        levels = [item.level for item in self.confirmed]
        return max(levels, key=lambda level: _ORDER.index(level)) if levels else None

    @property
    def points(self) -> int:
        level = self.level
        return level.points if level is not None else 0

    @property
    def reportable(self) -> bool:
        return self.points >= REPORTABLE_POINTS

    def next_level(self) -> ImpactLevel | None:
        """The next level worth trying, skipping ones already confirmed."""
        highest = self.level
        index = _ORDER.index(highest) + 1 if highest is not None else 0
        return _ORDER[index] if index < len(_ORDER) else None


def validate_attempt(attempt: ImpactAttempt) -> ImpactAttempt:
    if not attempt.evidence:
        raise ImpactError(
            f"impact level {attempt.level.value} needs evidence (a traffic pair or artifact)"
        )
    if attempt.level.state_changing and not attempt.rollback.strip():
        raise ImpactError(
            f"impact level {attempt.level.value} changes state and needs a rollback note"
        )
    if attempt.outcome not in ("confirmed", "refuted"):
        raise ImpactError(f"unknown impact outcome: {attempt.outcome!r}")
    return attempt
