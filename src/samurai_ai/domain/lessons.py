"""Evidence-gated lessons: what earlier tasks proved, not what a model guessed.

A lesson is admitted only with a cue (when it applies), an action (what to do),
and at least one evidence row pointing at the outcome that taught it. The shape
follows the harness2 Library rule quoted in ARTEX's `docs/rsi-lessons.md`.

Nothing here calls a model: lessons are composed deterministically from closed
finding verdicts, so a lesson can never be more confident than its evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

CUE_MAX = 160
ACTION_MAX = 320
DIGEST_MAX_LESSONS = 8
DIGEST_MAX_CHARS = 800


class LessonError(ValueError):
    pass


class LessonGateError(LessonError):
    """The lesson has no cue, no action, or no evidence behind it."""


class LessonNotFoundError(LookupError):
    pass


class LessonKind(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CAUTION = "caution"


class LessonStatus(StrEnum):
    CANDIDATE = "candidate"
    ADOPTED = "adopted"
    REJECTED = "rejected"


class LessonMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    INJECT = "inject"


class Lesson(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"lsn_{uuid4().hex[:12]}")
    workspace: str
    kind: LessonKind
    cue: str = Field(min_length=1, max_length=CUE_MAX)
    action: str = Field(min_length=1, max_length=ACTION_MAX)
    status: LessonStatus = LessonStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def compose_cue(workspace: str, subject: str) -> str:
    """Where the lesson applies. A blank subject leaves only the workspace."""
    subject = " ".join(subject.split())
    workspace = " ".join(workspace.split())
    if not subject:
        return workspace[:CUE_MAX]
    text = f"{workspace}: {subject}" if workspace else subject
    return text[:CUE_MAX]


def compose_action(kind: LessonKind, title: str, summary: str = "") -> str:
    """The reusable half of the lesson: what to do next time, not what happened."""
    title = " ".join(title.split())
    if kind is LessonKind.NEGATIVE:
        return f"Do not re-report without new mechanics: {title}"[:ACTION_MAX]
    body = " ".join(summary.split())
    prefix = "Confirmed " if kind is LessonKind.POSITIVE else "Recheck before reporting "
    text = f"{prefix}{title}"
    if body:
        text = f"{text}: {body}"
    return text[:ACTION_MAX]
