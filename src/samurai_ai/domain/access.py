"""Identity and roles.

Two rules keep this small. The role comes from the provider's claim, never from
a local guess — an unmapped claim is the weakest role, not a convenient one. And
the check sits on the command path, so no command has to remember it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AccessError(ValueError):
    pass


class NotAuthenticatedError(AccessError):
    pass


class ForbiddenError(AccessError):
    pass


class ProviderError(AccessError):
    pass


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANKS[self]


_RANKS = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

#: Commands an anonymous-but-not-configured install runs as.
LOCAL_PRINCIPAL_ROLE = Role.ADMIN


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)
    email: str = ""
    display: str = ""
    role: Role = Role.VIEWER
    issuer: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def label(self) -> str:
        return self.email or self.display or self.subject


class Session(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"ses_{uuid4().hex[:12]}")
    principal: Principal
    access_expires_at: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.ended_at is None


class DeviceCode(BaseModel):
    """What the operator is shown while the browser side happens."""

    model_config = ConfigDict(frozen=True)

    device_code: str
    user_code: str
    verification_uri: str
    interval: int = 5
    expires_in: int = 600


#: group or group.subcommand -> the weakest role that may run it.
#: Anything unlisted needs `operator`, so a new command is closed by default.
_MINIMUM: dict[str, Role] = {
    "auth": Role.VIEWER,
    "auth.login": Role.VIEWER,
    "auth.whoami": Role.VIEWER,
    "role": Role.VIEWER,
    "role.list": Role.VIEWER,
    "role.map": Role.ADMIN,
    "role.rm": Role.ADMIN,
    "operation": Role.OPERATOR,
    "operation.list": Role.VIEWER,
    "operation.show": Role.VIEWER,
    "operation.create": Role.ADMIN,
    "operation.transition": Role.ADMIN,
    "operation.fork": Role.ADMIN,
    "operation.export": Role.VIEWER,
    "scope.check": Role.VIEWER,
    "plan": Role.VIEWER,
    "plan.show": Role.VIEWER,
    "task": Role.OPERATOR,
    "task.list": Role.VIEWER,
    "finding": Role.OPERATOR,
    "finding.list": Role.VIEWER,
    "finding.show": Role.VIEWER,
    "report": Role.OPERATOR,
    "report.list": Role.VIEWER,
    "artifact": Role.OPERATOR,
    "artifact.list": Role.VIEWER,
    "artifact.show": Role.VIEWER,
    "approval": Role.OPERATOR,
    "approval.list": Role.VIEWER,
    "secret": Role.ADMIN,
    "model": Role.ADMIN,
    "mcp": Role.ADMIN,
    "sandbox.sign": Role.ADMIN,
    "events": Role.VIEWER,
    "eval.run": Role.ADMIN,
    "eval.compare": Role.VIEWER,
    "goal": Role.OPERATOR,
    "memory": Role.OPERATOR,
    "skill": Role.ADMIN,
    "playbook": Role.OPERATOR,
    "swarm": Role.OPERATOR,
    "agent": Role.OPERATOR,
    "headless": Role.OPERATOR,
    "rules": Role.VIEWER,
    "rules.list": Role.VIEWER,
    "rules.add": Role.ADMIN,
    "rules.rm": Role.ADMIN,
}


def minimum_role(path: str) -> Role:
    """The role a command path needs; unknown paths need an operator."""
    if path in _MINIMUM:
        return _MINIMUM[path]
    group = path.split(".", 1)[0]
    return _MINIMUM.get(group, Role.OPERATOR)


def require(principal: Principal, path: str) -> None:
    needed = minimum_role(path)
    if principal.role.rank < needed.rank:
        raise ForbiddenError(
            f"{path} needs the {needed.value} role; {principal.label} is "
            f"{principal.role.value}"
        )
