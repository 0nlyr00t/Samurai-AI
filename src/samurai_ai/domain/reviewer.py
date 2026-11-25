"""Configuration and bounded results of an independent operation reviewer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_REVIEW_NOTES = 4
MAX_REVIEW_NOTE_CHARS = 1000


class ReviewerError(ValueError):
    pass


class ReviewSeverity(StrEnum):
    INFO = "info"
    CONCERN = "concern"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class ReviewerConfig:
    model: str
    fallback: str | None = None


@dataclass(frozen=True)
class ReviewNote:
    severity: ReviewSeverity
    text: str


def parse_review_notes(raw: str) -> tuple[ReviewNote, ...]:
    """Read the reviewer's JSON, rejecting prose and malformed note shapes."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewerError("reviewer returned non-JSON output") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
        raise ReviewerError("reviewer response must contain a notes array")
    notes: list[ReviewNote] = []
    for item in payload["notes"][:MAX_REVIEW_NOTES]:
        if not isinstance(item, dict):
            raise ReviewerError("review note must be an object")
        try:
            severity = ReviewSeverity(str(item["severity"]))
            body = str(item["text"]).strip()
        except (KeyError, ValueError) as exc:
            raise ReviewerError(f"invalid review note: {exc}") from exc
        if not body:
            raise ReviewerError("review note text is empty")
        notes.append(ReviewNote(severity, body[:MAX_REVIEW_NOTE_CHARS]))
    return tuple(notes)
