"""A provisioning plan: everything that will change, fixed before it changes.

The plan is immutable and built from the recipe alone, so the dry-run and the run
execute the same commands. It carries the platform, the package, the exact argv,
the privilege requirement and the healthcheck; the operator approves *that* plan,
not a description of one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProvisionStatus(StrEnum):
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting-approval"
    INSTALLING = "installing"
    VERIFYING = "verifying"
    READY = "ready"
    FAILED = "failed"


class ProvisionPlan(BaseModel):
    """What an install of one tool does on one machine, command by command."""

    model_config = ConfigDict(frozen=True)

    tool_id: str
    recipe_version: int = Field(ge=1)
    platform: str
    arch: str
    package: str
    manager: str
    #: every argv the provisioner will run, in order, without a shell
    commands: tuple[tuple[str, ...], ...] = ()
    #: healthcheck argv run after the install and before the tool is declared ready
    healthcheck: tuple[str, ...] = ()
    privileged: bool = False
    requires_docker: bool = False
    requires_gpu: bool = False
    limitations: tuple[str, ...] = ()
    #: rolling apt/brew repositories: the version is recorded, not promised
    reproducible: bool = False
    manual_command: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_empty(self) -> bool:
        return not self.commands


class ProvisionState(BaseModel):
    """Per-tool state: the lock, the result and the installed version."""

    model_config = ConfigDict(validate_assignment=True)

    tool_id: str
    status: ProvisionStatus = ProvisionStatus.PLANNED
    recipe_version: int = Field(default=1, ge=1)
    installed_version: str = ""
    resolved_path: str = ""
    run_id: str = ""
    plan: ProvisionPlan | None = None
    attempts: int = Field(default=0, ge=0)
    error: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def locked(self) -> bool:
        return self.status in (ProvisionStatus.INSTALLING, ProvisionStatus.VERIFYING)

    @property
    def ready(self) -> bool:
        return self.status is ProvisionStatus.READY


class ProvisionError(ValueError):
    pass


class ProvisionLockedError(ProvisionError):
    """Another provision of the same tool holds the lock."""


class ProvisionRefusedError(ProvisionError):
    def __init__(self, reason: str, detail: str = "", manual: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.manual = manual
        super().__init__(detail or reason)


class ProvisionRun(BaseModel):
    """One executed plan: its commands, their output and the verdict."""

    model_config = ConfigDict(validate_assignment=True)

    run_id: str
    tool_id: str
    plan: ProvisionPlan
    status: ProvisionStatus = ProvisionStatus.PLANNED
    command_results: list[dict[str, object]] = Field(default_factory=list)
    healthcheck_output: str = ""
    error: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status is ProvisionStatus.READY


__all__ = [
    "ProvisionError",
    "ProvisionLockedError",
    "ProvisionPlan",
    "ProvisionRefusedError",
    "ProvisionRun",
    "ProvisionState",
    "ProvisionStatus",
]
