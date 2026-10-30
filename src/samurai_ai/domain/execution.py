from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.vault import SECRET_PLACEHOLDER

_TOOL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:\.[a-z0-9]+(?:-[a-z0-9]+)*)+\Z")


class ToolExecutionError(ValueError):
    pass


class ApprovalRequiredError(ToolExecutionError):
    def __init__(self, approval_id: str, message: str | None = None) -> None:
        self.approval_id = approval_id
        super().__init__(message or f"approval required before execution: {approval_id}")


class ToolNotFoundError(LookupError):
    pass


class ToolScopeError(ToolExecutionError):
    pass


class ToolStateError(ToolExecutionError):
    pass


class ToolValidationError(ToolExecutionError):
    pass


class ToolKind(StrEnum):
    LOCAL = "local"
    PROCESS = "process"
    HTTP = "http"
    SANDBOX = "sandbox"
    MCP = "mcp"


class ToolRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class OutputTrust(StrEnum):
    TRUSTED_OPERATOR = "trusted-operator"
    UNTRUSTED_EXTERNAL = "untrusted-external"


class NetworkAccess(StrEnum):
    """How a tool can reach the network.

    A plain boolean conflated "cannot reach the network" with "reaches it,
    but only through the scoped egress proxy", which made the UI report
    sandbox scanners as ``no-network``.

    NONE — no network at all. PROXIED — reachable only through the scoped
    egress proxy that enforces operation scope. DIRECT — the adapter opens a
    host socket itself and checks scope before connecting (the HTTP broker).
    """

    NONE = "none"
    PROXIED = "proxied"
    DIRECT = "direct"

    @property
    def reaches_network(self) -> bool:
        return self is not NetworkAccess.NONE


class ToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: int = Field(ge=1)
    kind: ToolKind
    description: str = ""
    risk_class: RiskClass
    #: Parameters only read: no state change on the host or on the target.
    #: Plan mode admits read-only calls only, so this flag is the gate — and it
    #: must stay honest, hence the risk-class coupling checked by the registry.
    read_only: bool = False
    network: NetworkAccess = NetworkAccess.NONE
    timeout_seconds: int = Field(default=30, ge=1)
    output_limit_bytes: int = Field(default=65536, ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    capabilities: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _TOOL_ID.fullmatch(value):
            raise ToolValidationError(f"tool id must be dotted kebab-case: {value!r}")
        return value


class ToolRun(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
    operation_id: str
    task_id: str | None = None
    tool_id: str
    tool_version: int = Field(ge=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: ToolRunStatus = ToolRunStatus.QUEUED
    exit_code: int | None = None
    recovery_action_id: str | None = None
    approval_id: str | None = None
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    redaction_count: int = Field(default=0, ge=0)
    injection_warnings: tuple[str, ...] = ()
    trust: OutputTrust = OutputTrust.UNTRUSTED_EXTERNAL
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def start(self) -> None:
        if self.status != ToolRunStatus.QUEUED:
            raise ToolStateError(
                f"tool run {self.id} cannot start from {self.status.value}"
            )
        self.status = ToolRunStatus.RUNNING
        self.started_at = datetime.now(UTC)
        self.version += 1

    def complete(
        self,
        *,
        exit_code: int,
        stdout_artifact_id: str | None = None,
        stderr_artifact_id: str | None = None,
        redaction_count: int = 0,
        injection_warnings: tuple[str, ...] = (),
    ) -> None:
        self._require_running(ToolRunStatus.COMPLETED)
        self.status = ToolRunStatus.COMPLETED
        self._finish(
            exit_code=exit_code,
            stdout_artifact_id=stdout_artifact_id,
            stderr_artifact_id=stderr_artifact_id,
            redaction_count=redaction_count,
            injection_warnings=injection_warnings,
        )

    def fail(
        self,
        *,
        exit_code: int | None = None,
        stdout_artifact_id: str | None = None,
        stderr_artifact_id: str | None = None,
        redaction_count: int = 0,
        injection_warnings: tuple[str, ...] = (),
    ) -> None:
        self._require_running(ToolRunStatus.FAILED)
        self.status = ToolRunStatus.FAILED
        self._finish(
            exit_code=exit_code,
            stdout_artifact_id=stdout_artifact_id,
            stderr_artifact_id=stderr_artifact_id,
            redaction_count=redaction_count,
            injection_warnings=injection_warnings,
        )

    def cancel(self) -> None:
        if self.status not in (ToolRunStatus.QUEUED, ToolRunStatus.RUNNING):
            raise ToolStateError(
                f"tool run {self.id} cannot cancel from {self.status.value}"
            )
        self.status = ToolRunStatus.CANCELLED
        self.finished_at = datetime.now(UTC)
        self.version += 1

    def mark_timeout(self, *, exit_code: int | None = None) -> None:
        self._require_running(ToolRunStatus.TIMEOUT)
        self.status = ToolRunStatus.TIMEOUT
        self.exit_code = exit_code
        self.finished_at = datetime.now(UTC)
        self.version += 1

    def _require_running(self, target: ToolRunStatus) -> None:
        if self.status != ToolRunStatus.RUNNING:
            raise ToolStateError(
                f"tool run {self.id} cannot move from "
                f"{self.status.value} to {target.value}"
            )

    def _finish(
        self,
        *,
        exit_code: int | None,
        stdout_artifact_id: str | None,
        stderr_artifact_id: str | None,
        redaction_count: int,
        injection_warnings: tuple[str, ...],
    ) -> None:
        self.exit_code = exit_code
        self.stdout_artifact_id = stdout_artifact_id
        self.stderr_artifact_id = stderr_artifact_id
        self.redaction_count = redaction_count
        self.injection_warnings = injection_warnings
        self.finished_at = datetime.now(UTC)
        self.version += 1


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run: ToolRun
    stdout: str = ""
    stderr: str = ""
    artifact_ids: tuple[str, ...] = ()
    trust: OutputTrust = OutputTrust.UNTRUSTED_EXTERNAL
    redaction_count: int = Field(default=0, ge=0)
    injection_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedactionResult:
    text: str
    count: int


@dataclass
class SecretRedactor:
    """Redacts registered secret values and common token patterns from text.

    Secrets are kept in memory only; they are never persisted to the
    database or appended to operation events.
    """

    _values: list[str] = field(default_factory=list)

    _PATTERNS: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)(\s*[:=]\s*)\S+"),
    )
    PLACEHOLDER: ClassVar[str] = "***REDACTED***"

    def register(self, value: str) -> None:
        if value and value not in self._values:
            self._values.append(value)
            self._values.sort(key=len, reverse=True)

    def redact(self, text: str) -> RedactionResult:
        count = 0
        result = text
        for value in self._values:
            occurrences = result.count(value)
            if occurrences:
                result = result.replace(value, self.PLACEHOLDER)
                count += occurrences
        stashed: list[str] = []

        def stash(match: re.Match[str]) -> str:
            stashed.append(match.group(0))
            return f"\x00{len(stashed) - 1}\x00"

        protected = SECRET_PLACEHOLDER.sub(stash, result)
        for pattern in self._PATTERNS:
            protected, replacements = pattern.subn(self._pattern_replacement, protected)
            count += replacements
        result = re.sub(
            "\x00(\\d+)\x00", lambda match: stashed[int(match.group(1))], protected
        )
        return RedactionResult(text=result, count=count)

    def _pattern_replacement(self, match: re.Match[str]) -> str:
        if match.lastindex and match.lastindex >= 2 and match.group(2) is not None:
            return f"{match.group(1)}{match.group(2)}{self.PLACEHOLDER}"
        return self.PLACEHOLDER


class PromptInjectionScanner:
    """Heuristic scanner flagging common prompt-injection phrases in tool output."""

    _PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "ignore-previous-instructions",
            re.compile(r"ignore\s+(all\s+)?(the\s+)?(previous|prior|above)\s+instructions?", re.I),
        ),
        (
            "system-prompt-override",
            re.compile(r"system\s*prompt|<\s*system\s*>|\[\s*system\s*\]", re.I),
        ),
        (
            "new-instructions",
            re.compile(r"(new|follow\s+these)\s+instructions?\s*:", re.I),
        ),
        (
            "disregard-instructions",
            re.compile(r"disregard\s+(all\s+)?(the\s+)?(previous|prior|above)", re.I),
        ),
        (
            "role-reassignment",
            re.compile(r"you\s+are\s+(now\s+)?(a|an|the)\s+\w+", re.I),
        ),
    )

    def scan(self, text: str) -> tuple[str, ...]:
        warnings: list[str] = []
        for label, pattern in self._PATTERNS:
            if pattern.search(text):
                warnings.append(label)
        return tuple(warnings)
