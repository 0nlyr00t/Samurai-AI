from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.scope import ScopeContract


class OperationMode(StrEnum):
    BUG_BOUNTY = "bug-bounty"
    CTF_HTB = "ctf-htb"
    RED_TEAM = "red-team"
    OPERATION_PLAN = "operation-plan"


class OperationStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    APPROVAL = "approval"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    REPORTING = "reporting"
    COMPLETE = "complete"
    PAUSED = "paused"
    BLOCKED = "blocked"


_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.DRAFT: frozenset({OperationStatus.PLANNING, OperationStatus.BLOCKED}),
    OperationStatus.PLANNING: frozenset(
        {OperationStatus.APPROVAL, OperationStatus.PAUSED, OperationStatus.BLOCKED}
    ),
    OperationStatus.APPROVAL: frozenset(
        {
            OperationStatus.PLANNING,
            OperationStatus.EXECUTION,
            OperationStatus.PAUSED,
            OperationStatus.BLOCKED,
        }
    ),
    OperationStatus.EXECUTION: frozenset(
        {OperationStatus.VERIFICATION, OperationStatus.PAUSED, OperationStatus.BLOCKED}
    ),
    OperationStatus.VERIFICATION: frozenset(
        {
            OperationStatus.EXECUTION,
            OperationStatus.REPORTING,
            OperationStatus.PAUSED,
            OperationStatus.BLOCKED,
        }
    ),
    OperationStatus.REPORTING: frozenset(
        {
            OperationStatus.VERIFICATION,
            OperationStatus.COMPLETE,
            OperationStatus.PAUSED,
            OperationStatus.BLOCKED,
        }
    ),
    OperationStatus.COMPLETE: frozenset(),
    OperationStatus.PAUSED: frozenset(
        {
            OperationStatus.PLANNING,
            OperationStatus.APPROVAL,
            OperationStatus.EXECUTION,
            OperationStatus.VERIFICATION,
            OperationStatus.REPORTING,
            OperationStatus.BLOCKED,
        }
    ),
    OperationStatus.BLOCKED: frozenset({OperationStatus.PLANNING, OperationStatus.PAUSED}),
}


class OperationTransitionError(ValueError):
    pass


class Operation(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"op_{uuid4().hex[:12]}")
    name: str = Field(min_length=1, max_length=200)
    objective: str = ""
    mode: OperationMode
    status: OperationStatus = OperationStatus.DRAFT
    scope: ScopeContract
    #: the harness has no internet: model calls must be local, tools that reach
    #: the outside are refused instead of failing halfway
    offline: bool = False
    #: a command prefix that runs a tool somewhere else — `prlctl exec '<uuid>'`,
    #: `ssh -J jump`. Network tools then execute on the host through it instead
    #: of in a container, which is how a VPN living in a VM is reachable at all.
    relay: str = Field(default="", max_length=400)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("operation name cannot be empty")
        return name

    def transition_to(self, target: OperationStatus) -> OperationStatus:
        if target not in _TRANSITIONS[self.status]:
            raise OperationTransitionError(
                f"transition from {self.status.value} to {target.value} is not allowed"
            )
        previous = self.status
        self.status = target
        self.version += 1
        self.updated_at = datetime.now(UTC)
        return previous


class OperationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    operation_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
