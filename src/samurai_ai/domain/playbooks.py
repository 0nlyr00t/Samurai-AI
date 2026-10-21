from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from samurai_ai.domain.kernel import PlanTask, RiskClass, StageGate


class PlaybookError(ValueError):
    pass


class PlaybookModeError(PlaybookError):
    pass


class ProgramRulesError(PlaybookError):
    pass


class AssetValidationError(PlaybookError):
    pass


class AssetScopeError(PlaybookError):
    pass


class DuplicateCandidateError(PlaybookError):
    def __init__(self, fingerprint: str, existing_finding_id: str) -> None:
        super().__init__(
            f"finding duplicates {existing_finding_id} (fingerprint {fingerprint[:12]}…); "
            "use --force to create anyway"
        )
        self.fingerprint = fingerprint
        self.existing_finding_id = existing_finding_id


class AssetKind(StrEnum):
    DOMAIN = "domain"
    URL = "url"
    IP = "ip"
    SERVICE = "service"


_WHITESPACE = re.compile(r"\s+")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _normalize_domain(value: str) -> str:
    host = value.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise AssetValidationError(f"invalid domain asset: {value!r}") from exc
    labels = host.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise AssetValidationError(f"invalid domain asset: {value!r}")
    return host


def _normalize_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise AssetValidationError(f"invalid ip asset: {value!r}") from exc


def _normalize_host(value: str) -> str:
    try:
        return _normalize_ip(value)
    except AssetValidationError:
        return _normalize_domain(value)


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise AssetValidationError(f"invalid url asset: {value!r}") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise AssetValidationError(f"invalid url asset: {value!r}")
    scheme = parts.scheme.lower()
    host = _normalize_domain(parts.hostname)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _normalize_service(value: str) -> str:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise AssetValidationError(f"invalid service asset (expected host:port): {value!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise AssetValidationError(f"invalid service port: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise AssetValidationError(f"service port out of range: {value!r}")
    return f"{_normalize_host(host)}:{port}"


def normalize_asset_value(kind: AssetKind, raw: str) -> str:
    value = raw.strip()
    if not value:
        raise AssetValidationError("asset value cannot be empty")
    if kind is AssetKind.DOMAIN:
        return _normalize_domain(value)
    if kind is AssetKind.IP:
        return _normalize_ip(value)
    if kind is AssetKind.URL:
        return _normalize_url(value)
    return _normalize_service(value)


def scope_target_for_asset(kind: AssetKind, value: str) -> str:
    if kind is AssetKind.SERVICE:
        return value.rpartition(":")[0]
    return value


def _squash(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip().lower())


def finding_fingerprint(title: str, asset: str, category: str) -> str:
    normalized = "\n".join((_squash(title), asset.strip().lower(), _squash(category)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class BountyProgramRules(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    operation_id: str
    policy_url: str = ""
    asset_types: tuple[AssetKind, ...] = tuple(AssetKind)
    safe_harbor: bool = False
    notes: str = ""
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("asset_types")
    @classmethod
    def validate_asset_types(cls, value: tuple[AssetKind, ...]) -> tuple[AssetKind, ...]:
        if not value:
            raise ProgramRulesError("program rules must allow at least one asset type")
        return tuple(dict.fromkeys(value))

    def describe(self) -> str:
        lines = [
            f"policy_url: {self.policy_url or '(not set)'}",
            f"asset_types: {', '.join(kind.value for kind in self.asset_types)}",
            f"safe_harbor: {'yes' if self.safe_harbor else 'no'}",
        ]
        if self.notes:
            lines.append(f"notes: {self.notes}")
        return "\n".join(lines)


class AssetLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"ast_{uuid4().hex[:12]}")
    operation_id: str
    kind: AssetKind
    value: str = Field(min_length=1)
    source: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AssetDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    added: tuple[AssetLedgerEntry, ...] = ()
    removed: tuple[AssetLedgerEntry, ...] = ()
    unchanged: tuple[AssetLedgerEntry, ...] = ()


class FindingFingerprintRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    operation_id: str
    fingerprint: str = Field(min_length=64, max_length=64)
    asset: str = Field(min_length=1)
    category: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlaybookTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    title: str
    tasks: tuple[PlanTask, ...]
    gates: tuple[StageGate, ...]


def bug_bounty_template() -> PlaybookTemplate:
    return PlaybookTemplate(
        name="bug-bounty",
        title="Bug Bounty engagement",
        tasks=(
            PlanTask(
                id="recon",
                title="Map the in-scope attack surface",
                risk_class=RiskClass.R0,
            ),
            PlanTask(
                id="asset-inventory",
                title="Record discovered assets into the ledger",
                risk_class=RiskClass.R0,
                depends_on=("recon",),
                required_artifacts=("recon-notes",),
            ),
            PlanTask(
                id="testing",
                title="Test recorded assets within program rules",
                risk_class=RiskClass.R2,
                depends_on=("asset-inventory",),
                required_artifacts=("asset-ledger",),
            ),
            PlanTask(
                id="validation",
                title="Validate candidate findings with evidence",
                risk_class=RiskClass.R1,
                depends_on=("testing",),
                required_artifacts=("finding-evidence",),
            ),
            PlanTask(
                id="reporting",
                title="Generate and export the bug bounty report",
                risk_class=RiskClass.R0,
                depends_on=("validation",),
                required_artifacts=("final-report",),
            ),
        ),
        gates=(
            StageGate(
                id="scope-locked",
                name="Program rules recorded",
                required_artifacts=("program-rules",),
            ),
            StageGate(
                id="recon-complete",
                name="Recon notes collected",
                required_artifacts=("recon-notes",),
            ),
            StageGate(
                id="testing-complete",
                name="Testing log collected",
                required_artifacts=("testing-log",),
            ),
            StageGate(
                id="ready-to-report",
                name="Final report generated",
                required_artifacts=("final-report",),
            ),
        ),
    )
