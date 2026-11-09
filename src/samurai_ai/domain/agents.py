from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.swarm import AgentSpecialization


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


class RequestToolProvisionCommand(BaseModel):
    """Widen the machine, not the scope: the id is the only input."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["request_tool_provision"]
    tool_id: str = Field(min_length=1, max_length=64)


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


class ProposeHypothesisCommand(BaseModel):
    """Put a testable hypothesis on the hunt board; category is optional."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["propose_hypothesis"]
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(default="", max_length=32)
    notes: str = Field(default="", max_length=2000)


class RecordImpactCommand(BaseModel):
    """Escalate a finding's impact; the ladder rules live in the service."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["record_impact"]
    finding_id: str = Field(min_length=1, max_length=64)
    level: Literal["read", "write", "auth-bypass", "data-export", "rce"]
    evidence: tuple[str, ...] = Field(min_length=1, max_length=8)
    #: how to undo it; required for every level except `read`
    rollback: str = Field(default="", max_length=1024)
    outcome: Literal["confirmed", "refuted"] = "confirmed"


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=200)
    recommended: bool = False


class OperatorQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$")
    question: str = Field(min_length=1, max_length=400)
    header: str = Field(default="", max_length=16)
    options: tuple[QuestionOption, ...] = Field(min_length=2, max_length=4)

    @field_validator("header", mode="before")
    @classmethod
    def _trim_header(cls, value: object) -> object:
        """A chip that is two characters long must not fail the whole turn."""
        return value[:16].rstrip() if isinstance(value, str) else value


class AskOperatorCommand(BaseModel):
    """Ask the operator mid-run; the answer arrives as a note for the next step."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ask_operator"]
    #: models batch four questions without blinking; three was a needless wall
    questions: tuple[OperatorQuestion, ...] = Field(min_length=1, max_length=4)
    #: seconds to wait; -1 waits until the operator answers or the run stops
    timeout: int = Field(default=120, ge=-1, le=3600)


class CompleteTaskCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["complete_task"]
    #: why a task ends without anything reportable — the explicit verdict the
    #: closing gate wants instead of a silent stop
    no_finding: str = Field(default="", max_length=300)


class FailCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["fail"]
    reason: str = Field(min_length=1, max_length=1024)


AgentCommand = Annotated[
    RequestToolRunCommand
    | RequestToolProvisionCommand
    | NoteCommand
    | ProposeFindingCommand
    | ProposeHypothesisCommand
    | RecordImpactCommand
    | AskOperatorCommand
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
    swarm_id: str | None = None
    specialization: AgentSpecialization = AgentSpecialization.GENERAL
    workspace: str | None = None
    skills: tuple[str, ...] = ()
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
    elif isinstance(command, ProposeHypothesisCommand):
        summary["title"] = command.title
        if command.category:
            summary["category"] = command.category
    elif isinstance(command, RecordImpactCommand):
        summary["finding_id"] = command.finding_id
        summary["level"] = command.level
    elif isinstance(command, AskOperatorCommand):
        summary["questions"] = [question.id for question in command.questions]
    elif isinstance(command, FailCommand):
        summary["reason"] = command.reason
    return summary
