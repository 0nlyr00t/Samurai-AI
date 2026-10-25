from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.kernel import PlanTask, RiskClass, StageGate
from samurai_ai.domain.playbooks import PlaybookTemplate
from samurai_ai.domain.reports import ReportFormat


class CtfError(ValueError):
    pass


class CtfModeError(CtfError):
    pass


class ChallengeExistsError(CtfError):
    pass


class FlagFormatError(CtfError):
    pass


class DuplicateFlagError(CtfError):
    pass


class WriteupStateError(CtfError):
    pass


class HypothesisStateError(CtfError):
    pass


class ChallengeNotFoundError(LookupError):
    pass


class HypothesisNotFoundError(LookupError):
    pass


class WriteupNotFoundError(LookupError):
    pass


class ChallengeCategory(StrEnum):
    WEB = "web"
    PWN = "pwn"
    REVERSE = "reverse"
    CRYPTO = "crypto"
    FORENSICS = "forensics"
    MISC = "misc"
    PRIVESC = "privesc"


class HypothesisStatus(StrEnum):
    OPEN = "open"
    TESTING = "testing"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    ABANDONED = "abandoned"


def flag_matches_format(value: str, pattern: str) -> bool:
    if not pattern:
        return True
    return re.fullmatch(pattern, value) is not None


class CtfChallenge(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    category: ChallengeCategory
    description: str = ""
    win_condition: str = ""
    flag_format: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("flag_format")
    @classmethod
    def validate_flag_format(cls, value: str) -> str:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise FlagFormatError(f"invalid flag format regex: {value!r}") from exc
        return value

    def accepts_flag(self, value: str) -> bool:
        return flag_matches_format(value, self.flag_format)

    def describe(self) -> str:
        lines = [
            f"name: {self.name}",
            f"category: {self.category.value}",
            f"win_condition: {self.win_condition or '(not set)'}",
            f"flag_format: {self.flag_format or '(any)'}",
        ]
        if self.description:
            lines.append(f"description: {self.description}")
        return "\n".join(lines)


class Hypothesis(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"hyp_{uuid4().hex[:12]}")
    operation_id: str
    title: str = Field(min_length=1, max_length=200)
    category: ChallengeCategory | None = None
    status: HypothesisStatus = HypothesisStatus.OPEN
    notes: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def begin_testing(self) -> None:
        if self.status is not HypothesisStatus.OPEN:
            raise HypothesisStateError(
                f"hypothesis {self.id} cannot enter testing from {self.status.value}"
            )
        self.status = HypothesisStatus.TESTING
        self._touch()

    def confirm(self, notes: str = "") -> None:
        if self.status is not HypothesisStatus.TESTING:
            raise HypothesisStateError(
                f"hypothesis {self.id} must be testing before confirmation, "
                f"not {self.status.value}"
            )
        self.status = HypothesisStatus.CONFIRMED
        self._apply_notes(notes)
        self._touch()

    def refute(self, notes: str = "") -> None:
        if self.status is not HypothesisStatus.TESTING:
            raise HypothesisStateError(
                f"hypothesis {self.id} must be testing before refutation, "
                f"not {self.status.value}"
            )
        self.status = HypothesisStatus.REFUTED
        self._apply_notes(notes)
        self._touch()

    def abandon(self, notes: str = "") -> None:
        if self.status in (HypothesisStatus.CONFIRMED, HypothesisStatus.REFUTED):
            raise HypothesisStateError(
                f"hypothesis {self.id} is already {self.status.value} "
                "and cannot be abandoned"
            )
        self.status = HypothesisStatus.ABANDONED
        self._apply_notes(notes)
        self._touch()

    def _apply_notes(self, notes: str) -> None:
        if notes:
            self.notes = notes

    def _touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)


class FlagRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"flag_{uuid4().hex[:12]}")
    operation_id: str
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1)
    proof_artifact_id: str | None = None
    submitted_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CtfWriteup(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"wr_{uuid4().hex[:12]}")
    operation_id: str
    format: ReportFormat
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str = Field(min_length=1)
    flag_ids: tuple[str, ...]
    hypothesis_count: int = Field(ge=0)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def ctf_template() -> PlaybookTemplate:
    return PlaybookTemplate(
        name="ctf-htb",
        title="CTF/HTB challenge",
        tasks=(
            PlanTask(
                id="recon",
                title="Enumerate the lab target and challenge files",
                risk_class=RiskClass.R0,
            ),
            PlanTask(
                id="hypothesis-loop",
                title="Propose and test solution hypotheses",
                risk_class=RiskClass.R1,
                depends_on=("recon",),
                required_artifacts=("recon-notes",),
            ),
            PlanTask(
                id="exploitation",
                title="Execute the confirmed solution path",
                risk_class=RiskClass.R2,
                depends_on=("hypothesis-loop",),
                required_artifacts=("solution-path",),
            ),
            PlanTask(
                id="flag-capture",
                title="Capture and record flags with proof",
                risk_class=RiskClass.R1,
                depends_on=("exploitation",),
                required_artifacts=("flag-proof",),
            ),
            PlanTask(
                id="writeup",
                title="Generate the writeup and replay",
                risk_class=RiskClass.R0,
                depends_on=("flag-capture",),
                required_artifacts=("final-writeup",),
            ),
        ),
        gates=(
            StageGate(
                id="challenge-locked",
                name="Challenge brief recorded",
                required_artifacts=("challenge-brief",),
            ),
            StageGate(
                id="solution-confirmed",
                name="Solution path confirmed",
                required_artifacts=("solution-path",),
            ),
            StageGate(
                id="flag-captured",
                name="Flag captured with proof",
                required_artifacts=("flag-proof",),
            ),
            StageGate(
                id="writeup-ready",
                name="Writeup generated",
                required_artifacts=("final-writeup",),
            ),
        ),
    )
