"""Live benchmark: the harness driven by a real model against a real target.

Unlike the scripted eval suites (which exercise the orchestrator with canned
answers), a benchmark runs the chat loop exactly as an operator would, then
measures what came out of it — time to the goal, how far the kill chain got,
how many runs failed, how many of those were transport noise, tokens and cost.
A number that moves when nothing works is worse than no number, so a transport
failure is counted apart from an ordinary failed command and a suite refuses to
start on a host it cannot reach.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from samurai_ai.domain.operations import OperationMode


class BenchError(RuntimeError):
    """The benchmark could not run as configured."""


class BenchVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class BenchProbe(BaseModel):
    """How to prove the target answers before spending a run on it."""

    model_config = ConfigDict(frozen=True)

    port: int = Field(default=22, ge=1, le=65535)
    timeout_seconds: float = Field(default=8.0, gt=0.0)


class BenchLimits(BaseModel):
    """The ceiling of one scenario; a benchmark that can run forever measures nothing."""

    model_config = ConfigDict(frozen=True)

    steps: int = Field(default=40, ge=1)
    seconds: int = Field(default=1800, ge=1)
    #: continue messages after a turn ends without the goal
    stretches: int = Field(default=3, ge=0)
    max_wall_seconds: float | None = Field(default=None, gt=0.0)
    #: share of failed runs that is still acceptable
    max_failed_rate: float | None = Field(default=0.35, ge=0.0, le=1.0)
    #: a benchmark must not be polluted by its own plumbing
    max_transport_failures: int | None = Field(default=0, ge=0)


class BenchDifficulty(StrEnum):
    """A box class, so a run can be compared only against its own kind."""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class BenchScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    mode: OperationMode = OperationMode.RED_TEAM
    difficulty: BenchDifficulty = BenchDifficulty.MEDIUM
    #: the operator's own task text, sent to the chat loop verbatim
    objective: str = Field(min_length=1)
    #: extra scope rules beside the target hosts given on the command line
    scope_includes: tuple[str, ...] = ()
    #: finish line for the goal judge; defaults to the objective
    finish_line: str = ""
    #: a host that is always up (127.0.0.1) needs no probe
    probe: BenchProbe | None = None
    limits: BenchLimits = Field(default_factory=BenchLimits)


class BenchSuite(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    note: str = ""
    scenarios: list[BenchScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> BenchSuite:
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise BenchError("scenario ids must be unique inside a suite")
        return self


class BenchMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    wall_seconds: float = Field(ge=0.0)
    steps_spent: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    tool_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    transport_failures: int = Field(ge=0)
    chain_phase: str = ""
    chain_index: int = Field(ge=0)
    goal_reached: bool = False
    open_hypotheses: int = Field(ge=0)
    closed_hypotheses: int = Field(ge=0)
    flags: int = Field(ge=0)
    artifacts: int = Field(ge=0)

    @property
    def failed_rate(self) -> float:
        return self.failed_runs / self.tool_runs if self.tool_runs else 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class BenchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    #: the box class, carried so a report can group and compare by it
    difficulty: str = ""
    verdict: BenchVerdict
    metrics: BenchMetrics | None = None
    failures: tuple[str, ...] = ()
    error: str | None = None


class BenchReport(BaseModel):
    """One suite run; the datable row of the public metric."""

    model_config = ConfigDict(frozen=True)

    suite: str
    suite_version: str
    samurai_version: str
    model_id: str = ""
    hosts: tuple[str, ...] = ()
    started_at: datetime
    finished_at: datetime
    results: list[BenchResult]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(
            result.verdict is BenchVerdict.PASS for result in self.results
        )


def judge(result_metrics: BenchMetrics, limits: BenchLimits) -> tuple[BenchVerdict, list[str]]:
    """Decide a scenario: the goal, the hypotheses, then the noise ceilings.

    A run that reached the goal with untested hypotheses is not finished work.
    """
    failures: list[str] = []
    if not result_metrics.goal_reached:
        failures.append("goal not reached")
    if result_metrics.open_hypotheses:
        failures.append(f"{result_metrics.open_hypotheses} hypotheses left untested")
    if limits.max_transport_failures is not None:
        if result_metrics.transport_failures > limits.max_transport_failures:
            failures.append(
                f"transport failures {result_metrics.transport_failures} "
                f"> {limits.max_transport_failures}"
            )
    if limits.max_failed_rate is not None and result_metrics.failed_rate > limits.max_failed_rate:
        failures.append(
            f"failed runs {result_metrics.failed_rate:.0%} > {limits.max_failed_rate:.0%}"
        )
    if (
        limits.max_wall_seconds is not None
        and result_metrics.wall_seconds > limits.max_wall_seconds
    ):
        failures.append(
            f"wall {result_metrics.wall_seconds:.0f}s > {limits.max_wall_seconds:.0f}s"
        )
    return (BenchVerdict.FAIL if failures else BenchVerdict.PASS), failures


__all__ = [
    "BenchDifficulty",
    "BenchError",
    "BenchLimits",
    "BenchMetrics",
    "BenchProbe",
    "BenchReport",
    "BenchResult",
    "BenchScenario",
    "BenchSuite",
    "BenchVerdict",
    "judge",
]
