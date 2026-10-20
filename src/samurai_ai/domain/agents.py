from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AgentError(ValueError):
    pass


class AgentRunNotFoundError(LookupError):
    pass


class AgentDepthError(AgentError):
    pass


class AgentPlanError(AgentError):
    pass


class AgentOutputError(AgentError):
    pass


class RequestToolRunCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["request_tool_run"]
    tool_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class NoteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["note"]
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(max_length=4096)


class ProposeFindingCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["propose_finding"]
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(default="", max_length=4096)
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    artifact_ids: tuple[str, ...] = ()


class CompleteTaskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["complete_task"]


class FailCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["fail"]
    reason: str = Field(min_length=1, max_length=1024)


AgentCommand = Annotated[
    RequestToolRunCommand
    | NoteCommand
    | ProposeFindingCommand
    | CompleteTaskCommand
    | FailCommand,
    Field(discriminator="type"),
]


class ExecutorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: tuple[AgentCommand, ...] = Field(min_length=1, max_length=16)


class PlannerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: tuple[str, ...] = Field(min_length=1, max_length=16)


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget-exceeded"


class AgentRun(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"agent_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str
    parent_run_id: str | None = None
    depth: int = Field(default=0, ge=0)
    status: AgentRunStatus = AgentRunStatus.RUNNING
    model_route: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "operator"
    finished_at: datetime | None = None

    def finish(self, status: AgentRunStatus) -> None:
        if self.status is not AgentRunStatus.RUNNING:
            raise AgentError(f"agent run {self.id} already finished: {self.status.value}")
        if status is AgentRunStatus.RUNNING:
            raise AgentError("cannot finish an agent run with status running")
        self.status = status
        self.finished_at = datetime.now(UTC)


def command_summary(command: AgentCommand) -> dict[str, Any]:
    summary: dict[str, Any] = {"type": command.type}
    if isinstance(command, RequestToolRunCommand):
        summary["tool_id"] = command.tool_id
    elif isinstance(command, NoteCommand):
        summary["key"] = command.key
    elif isinstance(command, ProposeFindingCommand):
        summary["title"] = command.title
        summary["severity"] = command.severity
        summary["artifact_ids"] = list(command.artifact_ids)
    elif isinstance(command, FailCommand):
        summary["reason"] = command.reason
    return summary
