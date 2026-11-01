from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class KernelError(ValueError):
    pass


class PlanValidationError(KernelError):
    pass


class TaskStateError(KernelError):
    pass


class GateStateError(KernelError):
    pass


class ApprovalStateError(KernelError):
    pass


class ArtifactError(KernelError):
    pass


class FindingStateError(KernelError):
    pass


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in-progress"
    BLOCKED = "blocked"
    DONE = "done"
    SKIPPED = "skipped"


class GateStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class RiskClass(StrEnum):
    R0 = "r0"
    R1 = "r1"
    R2 = "r2"
    R3 = "r3"
    R4 = "r4"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ArtifactKind(StrEnum):
    NOTE = "note"
    COMMAND_OUTPUT = "command-output"
    FILE = "file"
    SCREENSHOT = "screenshot"
    LOG = "log"
    OTHER = "other"


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATING = "validating"
    VALIDATED = "validated"
    FALSE_POSITIVE = "false-positive"
    DUPLICATE = "duplicate"
    REPORTED = "reported"
    FIXED = "fixed"


def _validate_kebab_id(value: str) -> str:
    if not _KEBAB.fullmatch(value):
        raise PlanValidationError(f"id must be kebab-case: {value!r}")
    return value


def _validate_requirements(value: tuple[str, ...]) -> tuple[str, ...]:
    requirements: list[str] = []
    for requirement in value:
        text = requirement.strip()
        if not text:
            raise PlanValidationError("artifact requirement cannot be empty")
        requirements.append(text)
    return tuple(requirements)


class PlanTask(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    risk_class: RiskClass = RiskClass.R0
    depends_on: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_kebab_id(value)

    @field_validator("required_artifacts")
    @classmethod
    def validate_required_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_requirements(value)


class StageGate(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str
    name: str = Field(min_length=1, max_length=200)
    status: GateStatus = GateStatus.PENDING
    required_artifacts: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_kebab_id(value)

    @field_validator("required_artifacts")
    @classmethod
    def validate_required_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_requirements(value)


class OperationPlan(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:12]}")
    operation_id: str
    title: str = Field(min_length=1, max_length=200)
    tasks: tuple[PlanTask, ...] = ()
    gates: tuple[StageGate, ...] = ()
    version: int = Field(default=1, ge=1)
    approved: bool = False
    is_current: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_structure(self) -> OperationPlan:
        task_ids = [task.id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise PlanValidationError("task ids must be unique within a plan")
        gate_ids = [gate.id for gate in self.gates]
        if len(set(gate_ids)) != len(gate_ids):
            raise PlanValidationError("gate ids must be unique within a plan")

        known = set(task_ids)
        for task in self.tasks:
            unknown = [dependency for dependency in task.depends_on if dependency not in known]
            if unknown:
                raise PlanValidationError(
                    f"task {task.id} depends on unknown tasks: {', '.join(unknown)}"
                )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str, chain: tuple[str, ...]) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                cycle = " -> ".join((*chain, task_id))
                raise PlanValidationError(f"task dependency cycle detected: {cycle}")
            visiting.add(task_id)
            task = next(item for item in self.tasks if item.id == task_id)
            for dependency in task.depends_on:
                visit(dependency, (*chain, task_id))
            visiting.discard(task_id)
            visited.add(task_id)

        for task in self.tasks:
            visit(task.id, ())
        return self

    def task_by_id(self, task_id: str) -> PlanTask | None:
        return next((task for task in self.tasks if task.id == task_id), None)

    def gate_by_id(self, gate_id: str) -> StageGate | None:
        return next((gate for gate in self.gates if gate.id == gate_id), None)

    def touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)


class Approval(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"apr_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str | None = None
    action: str = Field(min_length=1, max_length=200)
    reason: str = ""
    risk_class: RiskClass
    status: ApprovalStatus = ApprovalStatus.PENDING
    auto: bool = False
    requested_by: str = "system"
    decided_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    version: int = Field(default=1, ge=1)


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"art_{uuid4().hex[:12]}")
    operation_id: str
    kind: ArtifactKind
    summary: str = ""
    requirement: str | None = None
    task_id: str | None = None
    gate_id: str | None = None
    finding_id: str | None = None
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str = Field(min_length=1)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Finding(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"find_{uuid4().hex[:12]}")
    operation_id: str
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    severity: FindingSeverity = FindingSeverity.INFO
    status: FindingStatus = FindingStatus.CANDIDATE
    evidence_ids: tuple[str, ...] = ()
    #: Bumped whenever the evidence set changes; a report written against an older
    #: version is refused instead of silently describing stale evidence.
    evidence_version: int = Field(default=0, ge=0)
    remediation: str = ""
    duplicate_of: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def begin_validation(self) -> None:
        if self.status != FindingStatus.CANDIDATE:
            raise FindingStateError(
                f"finding {self.id} cannot enter validation from {self.status.value}"
            )
        self.status = FindingStatus.VALIDATING
        self._touch()

    def attach_evidence(self, artifact_id: str) -> None:
        if self.status not in (FindingStatus.CANDIDATE, FindingStatus.VALIDATING):
            raise FindingStateError(
                f"finding {self.id} cannot accept evidence in {self.status.value}"
            )
        if artifact_id not in self.evidence_ids:
            self.evidence_ids = (*self.evidence_ids, artifact_id)
        self._touch()

    def mark_fixed(self) -> None:
        """A retest proved the fix: only a reported or validated finding can get here."""
        if self.status not in (FindingStatus.VALIDATED, FindingStatus.REPORTED):
            raise FindingStateError(
                f"finding {self.id} cannot be marked fixed in {self.status.value}"
            )
        self.status = FindingStatus.FIXED
        self._touch()

    def mark_validated(self) -> None:
        if self.status != FindingStatus.VALIDATING:
            raise FindingStateError(
                f"finding {self.id} must be validating before validation, not {self.status.value}"
            )
        if not self.evidence_ids:
            raise FindingStateError(f"finding {self.id} requires evidence before validation")
        self.status = FindingStatus.VALIDATED
        self._touch()

    def mark_false_positive(self) -> None:
        if self.status not in (FindingStatus.CANDIDATE, FindingStatus.VALIDATING):
            raise FindingStateError(
                f"finding {self.id} cannot be marked false-positive in {self.status.value}"
            )
        self.status = FindingStatus.FALSE_POSITIVE
        self._touch()

    def mark_duplicate(self, original_id: str) -> None:
        if self.status not in (FindingStatus.CANDIDATE, FindingStatus.VALIDATING):
            raise FindingStateError(
                f"finding {self.id} cannot be marked duplicate in {self.status.value}"
            )
        if original_id == self.id:
            raise FindingStateError(f"finding {self.id} cannot duplicate itself")
        self.status = FindingStatus.DUPLICATE
        self.duplicate_of = original_id
        self._touch()

    def mark_reported(self) -> None:
        if self.status != FindingStatus.VALIDATED:
            raise FindingStateError(
                f"finding {self.id} must be validated before reporting, not {self.status.value}"
            )
        self.status = FindingStatus.REPORTED
        self._touch()

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)
