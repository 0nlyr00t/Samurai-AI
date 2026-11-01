"""Operator constraints on a task: the boundary that outranks heuristics.

A constraint is one declarative line — "do not touch /admin", "only /api/v2" —
kept apart from the plan so it can be added mid-task without a plan revision.
Constraints render above the expansion heuristics in the prompt: a declared
boundary has to win the tug-of-war against "look somewhere else".
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

TEXT_MAX = 200
MAX_CONSTRAINTS = 12


class ConstraintError(ValueError):
    pass


class ConstraintKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class Constraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"cst_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str
    kind: ConstraintKind
    text: str = Field(min_length=1, max_length=TEXT_MAX)
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
