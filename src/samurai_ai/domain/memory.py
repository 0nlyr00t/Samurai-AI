"""Campaign memory: cross-session, cross-operation operator knowledge.

Memory entries are workspace-scoped and deliberately NOT part of an
operation's evidence journal: they are curated operator knowledge
(methodologies, decisions, failure causes), retention-bounded and deletable
by design. Agents get read-only access via bounded, redacted prompt digests;
agents can never write memory (a prompt-injection barrier).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_MEMORY_BODY_CHARS = 8192
MAX_MEMORY_TITLE_CHARS = 200
MAX_MEMORY_TAGS = 16
DIGEST_MAX_ENTRIES = 10
DIGEST_MAX_BODY_CHARS = 300

_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class MemoryError(ValueError):
    pass


class MemoryNotFoundError(LookupError):
    pass


class MemoryKind(StrEnum):
    METHODOLOGY = "methodology"
    DECISION = "decision"
    FAILURE = "failure"
    NOTE = "note"


class MemoryEntry(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"mem_{uuid4().hex[:12]}")
    workspace: str
    kind: MemoryKind = MemoryKind.NOTE
    title: str = Field(min_length=1, max_length=MAX_MEMORY_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_MEMORY_BODY_CHARS)
    tags: tuple[str, ...] = Field(default_factory=tuple, max_length=MAX_MEMORY_TAGS)
    retention_days: int | None = Field(default=None, ge=1)
    version: int = Field(default=1, ge=1)
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str) -> str:
        if not _KEBAB.fullmatch(value):
            raise MemoryError(f"workspace must be kebab-case: {value!r}")
        return value

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.retention_days is None:
            return False
        moment = now or datetime.now(UTC)
        return moment >= self.updated_at + timedelta(days=self.retention_days)

    def touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now(UTC)
