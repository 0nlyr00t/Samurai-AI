"""Who is on an operation, what they may do there, and what they said.

Membership only ever adds reach: the effective level is the higher of the global
role and the level on the operation. A stricter per-operation level would make an
invited admin weaker than a guest, which nobody expects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from samurai_ai.domain.access import Principal, Role

BODY_MAX = 1000
#: How many notes a single target shows before the rest are read from the log.
NOTE_TAIL = 20


class TeamworkError(ValueError):
    pass


class NotAMemberError(TeamworkError):
    pass


class OperationLevel(StrEnum):
    OWNER = "owner"
    MEMBER = "member"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        return _LEVEL_RANKS[self]


_LEVEL_RANKS = {OperationLevel.VIEWER: 0, OperationLevel.MEMBER: 1, OperationLevel.OWNER: 2}

#: What a global role is worth inside an operation it is not a member of.
_ROLE_LEVEL = {Role.VIEWER: OperationLevel.VIEWER, Role.OPERATOR: OperationLevel.MEMBER}
#: Above this global rank, membership is not needed at all.
_ADMIN = Role.ADMIN


class Member(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    subject: str = Field(min_length=1)
    label: str = ""
    level: OperationLevel = OperationLevel.MEMBER
    added_by: str = ""
    added_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NoteTarget(StrEnum):
    FINDING = "finding"
    TASK = "task"


class OperationNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"note_{uuid4().hex[:12]}")
    operation_id: str
    target_kind: NoteTarget
    target_id: str
    body: str = Field(min_length=1, max_length=BODY_MAX)
    author: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def effective_level(principal: Principal, member: Member | None) -> OperationLevel | None:
    """The level a principal holds here, or None when they are not on it.

    Membership is the gate — otherwise every operator would see every campaign.
    Within an operation the global role is a floor, so a member with a viewer row
    who is a global operator still works, and nobody is ever downgraded.
    """
    if principal.role is _ADMIN:
        return OperationLevel.OWNER
    if member is None:
        return None
    floor = _ROLE_LEVEL.get(principal.role, OperationLevel.VIEWER)
    return member.level if member.level.rank > floor.rank else floor
