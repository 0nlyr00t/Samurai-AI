"""Custom skills: trusted, versioned instruction fragments for agent prompts.

A skill is operator-authored prompt content, so it is a prompt-injection
surface: installation requires an explicit trust decision (`--trusted`), the
content hash is recorded, and installed versions are immutable. Agents read
skills only as bounded, redacted prompt fragments; the command whitelist and
governance do not change.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SKILL_BODY_CHARS = 8192
MAX_SKILLS_PER_RUN = 4
PROMPT_MAX_SKILL_CHARS = 2000

_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class SkillError(ValueError):
    pass


class SkillNotFoundError(LookupError):
    pass


class SkillTrustError(SkillError):
    pass


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"skill_{uuid4().hex[:12]}")
    name: str
    version: int = Field(default=1, ge=1)
    body: str = Field(min_length=1, max_length=MAX_SKILL_BODY_CHARS)
    sha256: str = Field(min_length=64, max_length=64)
    trusted: bool = False
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _KEBAB.fullmatch(value):
            raise SkillError(f"skill name must be kebab-case: {value!r}")
        return value
