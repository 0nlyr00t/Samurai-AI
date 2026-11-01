"""Retests: a finding checked again, with a verdict that has to be earned.

A retest exists because "still broken" and "now fixed" are claims, not
impressions. `reproduced` needs the observation, `fixed` needs a control that
still behaves normally, and anything short of that is `inconclusive` — which is
a real answer, not a failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SUMMARY_MAX = 2000
EVIDENCE_MAX = 4000


class RetestError(ValueError):
    pass


class RetestNotFoundError(LookupError):
    pass


class RetestVerdict(StrEnum):
    REPRODUCED = "reproduced"
    FIXED = "fixed"
    INCONCLUSIVE = "inconclusive"


class Retest(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"rts_{uuid4().hex[:12]}")
    operation_id: str
    finding_id: str
    verdict: RetestVerdict | None = None
    summary: str = ""
    evidence: str = ""
    control: str = ""
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def open(self) -> bool:
        return self.verdict is None
