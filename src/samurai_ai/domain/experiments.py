"""Bounded experiments over shadow decision-layer genes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4

MAX_EXPERIMENT_RUNS = 20
MAX_EXPERIMENT_DESCRIPTION = 1000
_METRIC = re.compile(r"(?m)^METRIC\s+([\w.µ-]+)=([^\s]+)\s*$")


class ExperimentError(ValueError):
    pass


class MetricDirection(StrEnum):
    LOWER = "lower"
    HIGHER = "higher"


class ExperimentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExperimentConfig:
    id: str
    metric: str
    direction: MetricDirection
    limit: int
    command_secret: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[\w.µ-]{1,64}", self.metric):
            raise ExperimentError("invalid experiment metric name")
        if not 1 <= self.limit <= MAX_EXPERIMENT_RUNS:
            raise ExperimentError(f"experiment limit must be 1-{MAX_EXPERIMENT_RUNS}")


@dataclass
class ExperimentRun:
    id: str
    iteration: int
    tool_run_id: str
    exit_code: int | None
    value: float | None
    gene_id: str | None = None
    status: str | None = None
    description: str = ""


@dataclass
class ExperimentState:
    config: ExperimentConfig
    status: ExperimentStatus = ExperimentStatus.ACTIVE
    runs: list[ExperimentRun] = field(default_factory=list)
    best_metric: float | None = None
    best_run_id: str | None = None
    best_gene_id: str | None = None
    open_candidate: str | None = None

    @property
    def logged_count(self) -> int:
        return sum(run.status is not None for run in self.runs)

    @property
    def pending_run(self) -> ExperimentRun | None:
        return next((run for run in reversed(self.runs) if run.status is None), None)

    @property
    def remaining(self) -> int:
        return max(0, self.config.limit - self.logged_count)


def new_experiment_id() -> str:
    return f"exp_{uuid4().hex[:12]}"


def new_experiment_run_id() -> str:
    return f"xrun_{uuid4().hex[:12]}"


def parse_metric(stdout: str, name: str) -> float | None:
    value: float | None = None
    for match in _METRIC.finditer(stdout):
        if match.group(1) != name:
            continue
        try:
            parsed = float(match.group(2))
        except ValueError:
            value = None
            continue
        value = parsed if math.isfinite(parsed) else None
    return value


def improved(value: float, best: float | None, direction: MetricDirection) -> bool:
    if best is None:
        return True
    return value < best if direction is MetricDirection.LOWER else value > best
