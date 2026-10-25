from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

MAX_SWARM_WORKERS = 4


class SwarmError(ValueError):
    pass


class SwarmNotFoundError(LookupError):
    pass


class SwarmTaskError(SwarmError):
    pass


class AgentSpecialization(StrEnum):
    GENERAL = "general"
    RECON = "recon"
    TESTING = "testing"
    ANALYSIS = "analysis"


class SwarmStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SwarmRun(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: f"swarm_{uuid4().hex[:12]}")
    operation_id: str
    status: SwarmStatus = SwarmStatus.RUNNING
    workers: int = Field(ge=1, le=MAX_SWARM_WORKERS)
    task_ids: tuple[str, ...] = Field(min_length=1)
    model_route: tuple[str, ...] = Field(min_length=1)
    created_by: str = "operator"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    def finish(self, status: SwarmStatus) -> None:
        if self.status is not SwarmStatus.RUNNING:
            raise SwarmError(f"swarm {self.id} already finished: {self.status.value}")
        if status is SwarmStatus.RUNNING:
            raise SwarmError("cannot finish a swarm with status running")
        self.status = status
        self.finished_at = datetime.now(UTC)
