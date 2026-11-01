"""Captured HTTP pairs as evidence: a finding hangs on them with a role.

A request/response pair is the closest thing to proof a web finding has, so it
is stored as a row with its own hash, bound to a finding under an explicit role
(baseline, proof, verification, supporting) and reachable in segments instead of
one giant blob.

Binding changes bump the finding's evidence version; a report written against an
older version is refused, so a report cannot silently describe evidence that has
since moved.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

#: Bodies are cut, never trusted to be small.
BODY_MAX = 32_768
#: A read returns at most this much per call, so a caller can page.
SEGMENT_MAX = 8_192
MAX_BINDINGS = 24
URL_MAX = 2048


class TrafficError(ValueError):
    pass


class TrafficNotFoundError(LookupError):
    pass


class TrafficRole(StrEnum):
    BASELINE = "baseline"
    PROOF = "proof"
    VERIFICATION = "verification"
    SUPPORTING = "supporting"


class TrafficPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"trf_{uuid4().hex[:12]}")
    operation_id: str
    method: str = Field(min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=URL_MAX)
    status: int = Field(ge=0, le=999)
    request_body: str = ""
    response_body: str = ""
    truncated: bool = False
    sha256: str = Field(min_length=64, max_length=64)
    created_by: str = "agent"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TrafficBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    traffic_id: str
    role: TrafficRole
    note: str = ""


def digest(method: str, url: str, status: int, request_body: str, response_body: str) -> str:
    raw = "\n".join((method.upper(), url, str(status), request_body, response_body))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clip_body(text: str) -> tuple[str, bool]:
    """Cut a body to the limit; the flag says whether anything was lost."""
    if len(text) <= BODY_MAX:
        return text, False
    return text[:BODY_MAX], True


def segment(pair: TrafficPair, side: str, offset: int) -> tuple[str, int]:
    """One slice of a body and the offset to continue from (0 at the end)."""
    body = pair.request_body if side == "request" else pair.response_body
    start = max(0, offset)
    part = body[start : start + SEGMENT_MAX]
    following = start + len(part)
    return part, (following if following < len(body) else 0)
