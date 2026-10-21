from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ReportError(ValueError):
    pass


class ReportStateError(ReportError):
    pass


class ReportScopeViolationError(ReportError):
    pass


class ReportNotFoundError(LookupError):
    pass


class ReportFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


class ManifestFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class ReportManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_id: str
    operation_id: str
    files: tuple[ManifestFile, ...]
    finding_ids: tuple[str, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BountyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"rep_{uuid4().hex[:12]}")
    operation_id: str
    format: ReportFormat
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    path: str = Field(min_length=1)
    finding_ids: tuple[str, ...]
    asset_count: int = Field(ge=0)
    created_by: str = "system"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
