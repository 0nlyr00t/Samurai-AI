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


class PlanningError(ValueError):
    pass


class PlanningModeError(PlanningError):
    pass


class ScenarioExistsError(PlanningError):
    pass


class TechniqueFormatError(PlanningError):
    pass


class PlannedTestStateError(PlanningError):
    pass


class PlannedTestDependencyError(PlanningError):
    pass


class RunbookStateError(PlanningError):
    pass


class ScenarioNotFoundError(LookupError):
    pass


class PlannedTestNotFoundError(LookupError):
    pass


class RunbookNotFoundError(LookupError):
    pass


class KillChainPhase(StrEnum):
    RECONNAISSANCE = "reconnaissance"
    WEAPONIZATION = "weaponization"
    DELIVERY = "delivery"
    EXPLOITATION = "exploitation"
    INSTALLATION = "installation"
    COMMAND_AND_CONTROL = "command-and-control"
    ACTIONS_ON_OBJECTIVES = "actions-on-objectives"


class PlannedTestStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RETIRED = "retired"


_TECHNIQUE_ID = re.compile(r"T\d{4}(\.\d{3})?\Z")


class PlanScenario(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    schedule: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def describe(self) -> str:
        lines = [f"name: {self.name}"]
        if self.description:
            lines.append(f"description: {self.description}")
        lines.append(f"schedule: {self.schedule or '(not set)'}")
        return "\n".join(lines)


class PlannedTest(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"tc_{uuid4().hex[:12]}")
    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    technique_id: str
    kill_chain_phase: KillChainPhase
    tactic: str | None = None
    expected_telemetry: str = ""
    detection_expectation: str = ""
    depends_on: tuple[str, ...] = ()
    stop_before: bool = False
    status: PlannedTestStatus = PlannedTestStatus.DRAFT
    notes: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("technique_id")
    @classmethod
    def validate_technique_id(cls, value: str) -> str:
        if not _TECHNIQUE_ID.fullmatch(value):
            raise TechniqueFormatError(
                f"invalid ATT&CK technique id: {value!r} (expected T1234 or T1234.001)"
            )
        return value

    @field_validator("tactic")
    @classmethod
    def validate_tactic(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_tactic(value):
            raise TechniqueFormatError(
                f"invalid ATT&CK tactic: {value!r} (expected one of ATTACK_TACTICS)"
            )
        return value

    @field_validator("depends_on")
    @classmethod
    def validate_depends_on(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value))

    def mark_ready(self, notes: str = "") -> None:
        if self.status is not PlannedTestStatus.DRAFT:
            raise PlannedTestStateError(
                f"test case {self.id} cannot become ready from {self.status.value}"
            )
        self.status = PlannedTestStatus.READY
        self._apply_notes(notes)
        self._touch()

    def retire(self, notes: str = "") -> None:
        if self.status is PlannedTestStatus.RETIRED:
            raise PlannedTestStateError(
                f"test case {self.id} is already retired"
            )
        self.status = PlannedTestStatus.RETIRED
        self._apply_notes(notes)
        self._touch()

    def _apply_notes(self, notes: str) -> None:
        if notes:
            self.notes = notes

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)


class Runbook(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"rb_{uuid4().hex[:12]}")
    operation_id: str
    format: ReportFormat
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str = Field(min_length=1)
    test_case_ids: tuple[str, ...]
    stop_points: int = Field(ge=0)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def planning_template() -> PlaybookTemplate:
    return PlaybookTemplate(
        name="operation-plan",
        title="Operation planning scenario",
        tasks=(
            PlanTask(
                id="scenario-draft",
                title="Draft the scenario and win conditions",
                risk_class=RiskClass.R0,
            ),
            PlanTask(
                id="mapping",
                title="Map the scenario to ATT&CK and the kill chain",
                risk_class=RiskClass.R0,
                depends_on=("scenario-draft",),
                required_artifacts=("scenario-brief",),
            ),
            PlanTask(
                id="test-design",
                title="Design test cases with detection expectations",
                risk_class=RiskClass.R0,
                depends_on=("mapping",),
                required_artifacts=("attack-mapping",),
            ),
            PlanTask(
                id="review",
                title="Review test cases and stop points",
                risk_class=RiskClass.R1,
                depends_on=("test-design",),
                required_artifacts=("test-cases",),
            ),
            PlanTask(
                id="runbook",
                title="Generate and export the runbook",
                risk_class=RiskClass.R0,
                depends_on=("review",),
                required_artifacts=("final-runbook",),
            ),
        ),
        gates=(
            StageGate(
                id="scenario-locked",
                name="Scenario brief recorded",
                required_artifacts=("scenario-brief",),
            ),
            StageGate(
                id="mapping-complete",
                name="ATT&CK and kill chain mapping recorded",
                required_artifacts=("attack-mapping",),
            ),
            StageGate(
                id="tests-ready",
                name="Test cases reviewed",
                required_artifacts=("test-cases",),
            ),
            StageGate(
                id="runbook-ready",
                name="Runbook generated",
                required_artifacts=("final-runbook",),
            ),
        ),
    )
