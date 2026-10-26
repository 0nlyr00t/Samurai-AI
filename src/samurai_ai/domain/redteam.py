from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.attack import is_valid_tactic
from samurai_ai.domain.kernel import PlanTask, RiskClass, StageGate
from samurai_ai.domain.playbooks import PlaybookTemplate
from samurai_ai.domain.reports import ReportFormat


class RedTeamError(ValueError):
    pass


class RedTeamModeError(RedTeamError):
    pass


class RoeExistsError(RedTeamError):
    pass


class TechniqueIdError(RedTeamError):
    pass


class AttackPathError(RedTeamError):
    pass


class AttackStepStateError(RedTeamError):
    pass


class CheckpointStateError(RedTeamError):
    pass


class DebriefStateError(RedTeamError):
    pass


class RoeNotFoundError(LookupError):
    pass


class AttackStepNotFoundError(LookupError):
    pass


class CheckpointNotFoundError(LookupError):
    pass


class DebriefNotFoundError(LookupError):
    pass


class AttackStepStatus(StrEnum):
    PLANNED = "planned"
    EXECUTING = "executing"
    EXECUTED = "executed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class DetectionStatus(StrEnum):
    PENDING = "pending"
    DETECTED = "detected"
    MISSED = "missed"
    NOT_OBSERVED = "not-observed"


class CheckpointStatus(StrEnum):
    PENDING = "pending"
    REACHED = "reached"


_TECHNIQUE_ID = re.compile(r"T\d{4}(\.\d{3})?\Z")


class RoeDocument(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    operation_id: str
    threat_profile: str = Field(min_length=1)
    objectives: tuple[str, ...] = Field(min_length=1)
    constraints: str = ""
    success_criteria: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def describe(self) -> str:
        lines = [
            f"threat_profile: {self.threat_profile}",
            "objectives:",
            *(f"- {objective}" for objective in self.objectives),
            f"constraints: {self.constraints or '(none)'}",
            f"success_criteria: {self.success_criteria or '(not set)'}",
        ]
        return "\n".join(lines)


class AttackStep(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"step_{uuid4().hex[:12]}")
    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    technique_id: str
    tactic: str | None = None
    expected_telemetry: str = ""
    detection_criteria: str = ""
    status: AttackStepStatus = AttackStepStatus.PLANNED
    detection: DetectionStatus = DetectionStatus.PENDING
    depends_on: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    notes: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("technique_id")
    @classmethod
    def validate_technique_id(cls, value: str) -> str:
        if not _TECHNIQUE_ID.fullmatch(value):
            raise TechniqueIdError(
                f"invalid ATT&CK technique id: {value!r} (expected T1234 or T1234.001)"
            )
        return value

    @field_validator("tactic")
    @classmethod
    def validate_tactic(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_tactic(value):
            raise TechniqueIdError(
                f"invalid ATT&CK tactic: {value!r} (expected one of ATTACK_TACTICS)"
            )
        return value

    @field_validator("depends_on")
    @classmethod
    def validate_depends_on(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value))

    def begin_execution(self) -> None:
        if self.status is not AttackStepStatus.PLANNED:
            raise AttackStepStateError(
                f"attack step {self.id} cannot start from {self.status.value}"
            )
        self.status = AttackStepStatus.EXECUTING
        self._touch()

    def complete(self, notes: str = "") -> None:
        if self.status is not AttackStepStatus.EXECUTING:
            raise AttackStepStateError(
                f"attack step {self.id} must be executing before completion, "
                f"not {self.status.value}"
            )
        self.status = AttackStepStatus.EXECUTED
        self._apply_notes(notes)
        self._touch()

    def block(self, notes: str = "") -> None:
        if self.status not in (AttackStepStatus.PLANNED, AttackStepStatus.EXECUTING):
            raise AttackStepStateError(
                f"attack step {self.id} cannot be blocked in {self.status.value}"
            )
        self.status = AttackStepStatus.BLOCKED
        self._apply_notes(notes)
        self._touch()

    def skip(self, notes: str = "") -> None:
        if self.status is not AttackStepStatus.PLANNED:
            raise AttackStepStateError(
                f"attack step {self.id} can only be skipped while planned, "
                f"not {self.status.value}"
            )
        self.status = AttackStepStatus.SKIPPED
        self._apply_notes(notes)
        self._touch()

    def record_detection(self, status: DetectionStatus, notes: str = "") -> None:
        if self.status is not AttackStepStatus.EXECUTED:
            raise AttackStepStateError(
                f"attack step {self.id} must be executed before recording detection, "
                f"not {self.status.value}"
            )
        if status is DetectionStatus.PENDING:
            raise AttackStepStateError("detection status cannot be reset to pending")
        if self.detection is not DetectionStatus.PENDING:
            raise AttackStepStateError(
                f"attack step {self.id} detection already recorded as "
                f"{self.detection.value}"
            )
        self.detection = status
        self._apply_notes(notes)
        self._touch()

    def attach_evidence(self, artifact_id: str) -> None:
        if artifact_id not in self.evidence_ids:
            self.evidence_ids = (*self.evidence_ids, artifact_id)
        self._touch()

    def _apply_notes(self, notes: str) -> None:
        if notes:
            self.notes = notes

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)


class Checkpoint(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"chk_{uuid4().hex[:12]}")
    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    criterion: str = ""
    status: CheckpointStatus = CheckpointStatus.PENDING
    notes: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def reach(self, notes: str = "") -> None:
        if self.status is not CheckpointStatus.PENDING:
            raise CheckpointStateError(
                f"checkpoint {self.id} is already {self.status.value}"
            )
        self.status = CheckpointStatus.REACHED
        if notes:
            self.notes = notes
        self.version += 1
        self.updated_at = datetime.now(UTC)


class Debrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"deb_{uuid4().hex[:12]}")
    operation_id: str
    format: ReportFormat
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str = Field(min_length=1)
    step_count: int = Field(ge=0)
    open_gaps: tuple[str, ...] = ()
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def red_team_template() -> PlaybookTemplate:
    return PlaybookTemplate(
        name="red-team",
        title="Red Team engagement",
        tasks=(
            PlanTask(
                id="roe-review",
                title="Review and approve the rules of engagement",
                risk_class=RiskClass.R0,
            ),
            PlanTask(
                id="path-planning",
                title="Plan the attack path with ATT&CK mapping",
                risk_class=RiskClass.R0,
                depends_on=("roe-review",),
                required_artifacts=("roe",),
            ),
            PlanTask(
                id="execution",
                title="Execute attack steps within the ROE",
                risk_class=RiskClass.R3,
                depends_on=("path-planning",),
                required_artifacts=("attack-path",),
            ),
            PlanTask(
                id="detection-review",
                title="Review detection outcomes against expectations",
                risk_class=RiskClass.R1,
                depends_on=("execution",),
                required_artifacts=("detection-log",),
            ),
            PlanTask(
                id="debrief",
                title="Generate the operation debrief",
                risk_class=RiskClass.R0,
                depends_on=("detection-review",),
                required_artifacts=("final-debrief",),
            ),
        ),
        gates=(
            StageGate(
                id="roe-approved",
                name="ROE approved",
                required_artifacts=("roe",),
            ),
            StageGate(
                id="path-approved",
                name="Attack path approved",
                required_artifacts=("attack-path",),
            ),
            StageGate(
                id="execution-finished",
                name="Execution log collected",
                required_artifacts=("execution-log",),
            ),
            StageGate(
                id="debrief-ready",
                name="Debrief generated",
                required_artifacts=("final-debrief",),
            ),
        ),
    )
