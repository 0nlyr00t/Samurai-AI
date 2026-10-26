from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from statistics import median
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from samurai_ai.domain.kernel import OperationPlan, RiskClass, TaskStatus
from samurai_ai.domain.operations import OperationEvent, OperationMode


class EvalError(ValueError):
    pass


class EvalSuiteError(EvalError):
    pass


class EvalScenarioStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class EvalTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    risk_class: RiskClass = RiskClass.R1


class EvalProviderScript(BaseModel):
    model_config = ConfigDict(frozen=True)

    responses: list[str] = Field(min_length=1)
    latency_ms: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=40, ge=0)
    completion_tokens: int = Field(default=8, ge=0)


class EvalExpectations(BaseModel):
    model_config = ConfigDict(frozen=True)

    expect_solve: bool = True
    min_tasks_done: int | None = Field(default=None, ge=0)
    max_invalid_actions: int | None = Field(default=None, ge=0)
    max_interventions: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0.0)
    max_wall_seconds: float | None = Field(default=None, ge=0.0)
    max_false_positives: int | None = Field(default=None, ge=0)


class EvalScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    mode: OperationMode = OperationMode.BUG_BOUNTY
    scope_includes: tuple[str, ...] = Field(min_length=1)
    tasks: list[EvalTask] = Field(min_length=1)
    script: EvalProviderScript
    workers: int = Field(default=1, ge=1, le=4)
    model_id: str = "eval/agent@v1"
    expectations: EvalExpectations = Field(default_factory=EvalExpectations)

    @model_validator(mode="after")
    def _unique_task_ids(self) -> EvalScenario:
        ids = [task.id for task in self.tasks]
        if len(set(ids)) != len(ids):
            raise EvalSuiteError(f"scenario {self.id}: duplicate task ids")
        return self


class EvalSuite(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    scenarios: list[EvalScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_scenario_ids(self) -> EvalSuite:
        ids = [scenario.id for scenario in self.scenarios]
        if len(set(ids)) != len(ids):
            raise EvalSuiteError(f"suite {self.name}: duplicate scenario ids")
        return self


class ScenarioMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    solved: bool
    tasks_done: int = Field(ge=0)
    tasks_total: int = Field(ge=0)
    validation_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    findings_created: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    wall_seconds: float = Field(ge=0.0)
    interventions: int = Field(ge=0)
    invalid_actions: int = Field(ge=0)
    false_positives: int = Field(ge=0)


class ScenarioResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    status: EvalScenarioStatus
    metrics: ScenarioMetrics | None = None
    expectation_failures: tuple[str, ...] = ()
    error: str | None = None


class SuiteAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    solve_rate: float = Field(ge=0.0, le=1.0)
    validation_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errored: int = Field(ge=0)
    total_cost_usd: float = Field(ge=0.0)
    total_tokens: int = Field(ge=0)
    total_wall_seconds: float = Field(ge=0.0)
    total_invalid_actions: int = Field(ge=0)
    total_interventions: int = Field(ge=0)
    total_false_positives: int = Field(ge=0)


class SuiteReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite: str
    suite_version: str
    samurai_version: str
    config: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    results: list[ScenarioResult]
    aggregate: SuiteAggregate

    @property
    def passed(self) -> bool:
        return all(result.status is EvalScenarioStatus.PASSED for result in self.results)


class ScenarioComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    scenario_id: str
    baseline: ScenarioMetrics | None
    current: ScenarioMetrics | None
    regressions: tuple[str, ...] = ()


class ComparisonResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_suite: str
    current_suite: str
    scenarios: list[ScenarioComparison]
    regressions: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.regressions


def metrics_from_events(
    events: list[OperationEvent],
    plan: OperationPlan | None,
    *,
    wall_seconds: float,
) -> ScenarioMetrics:
    tasks_total = 0
    tasks_done = 0
    if plan is not None:
        tasks_total = len(plan.tasks)
        tasks_done = sum(1 for task in plan.tasks if task.status is TaskStatus.DONE)
    model_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    cost = 0.0
    invalid_actions = 0
    interventions = 0
    false_positives = 0
    findings_created = 0
    validated = 0
    reported = 0
    for event in events:
        payload = event.payload
        if event.event_type == "model.call":
            model_calls += 1
            prompt_tokens += int(payload.get("prompt_tokens") or 0)
            completion_tokens += int(payload.get("completion_tokens") or 0)
            cost += float(payload.get("estimated_cost_usd") or 0.0)
        elif event.event_type == "agent.command.rejected":
            invalid_actions += 1
        elif event.event_type == "approval.requested":
            interventions += 1
        elif event.event_type == "finding.created":
            findings_created += 1
        elif event.event_type == "finding.validated":
            validated += 1
        elif event.event_type == "finding.reported":
            reported += 1
        elif event.event_type == "finding.marked-false-positive":
            false_positives += 1
    validation_rate = None
    if reported > 0:
        validation_rate = round(validated / reported, 4)
    return ScenarioMetrics(
        solved=plan is not None and tasks_total > 0 and tasks_done == tasks_total,
        tasks_done=tasks_done,
        tasks_total=tasks_total,
        validation_rate=validation_rate,
        findings_created=findings_created,
        model_calls=model_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=round(cost, 6),
        wall_seconds=round(wall_seconds, 3),
        interventions=interventions,
        invalid_actions=invalid_actions,
        false_positives=false_positives,
    )


def merge_metrics(runs: list[ScenarioMetrics]) -> ScenarioMetrics:
    """Median-merge repeated runs of the same scenario."""
    if len(runs) == 1:
        return runs[0]
    rates = [run.validation_rate for run in runs if run.validation_rate is not None]
    return ScenarioMetrics(
        solved=all(run.solved for run in runs),
        tasks_done=int(median(run.tasks_done for run in runs)),
        tasks_total=runs[0].tasks_total,
        validation_rate=round(median(rates), 4) if rates else None,
        findings_created=int(median(run.findings_created for run in runs)),
        model_calls=int(median(run.model_calls for run in runs)),
        prompt_tokens=int(median(run.prompt_tokens for run in runs)),
        completion_tokens=int(median(run.completion_tokens for run in runs)),
        cost_usd=round(float(median(run.cost_usd for run in runs)), 6),
        wall_seconds=round(float(median(run.wall_seconds for run in runs)), 3),
        interventions=int(median(run.interventions for run in runs)),
        invalid_actions=int(median(run.invalid_actions for run in runs)),
        false_positives=int(median(run.false_positives for run in runs)),
    )


def check_expectations(
    metrics: ScenarioMetrics, expectations: EvalExpectations
) -> tuple[str, ...]:
    failures: list[str] = []
    if metrics.solved is not expectations.expect_solve:
        failures.append(
            f"solved={metrics.solved} but expect_solve={expectations.expect_solve}"
        )
    checks = (
        ("min_tasks_done", metrics.tasks_done, expectations.min_tasks_done, min),
        (
            "max_invalid_actions",
            metrics.invalid_actions,
            expectations.max_invalid_actions,
            max,
        ),
        ("max_interventions", metrics.interventions, expectations.max_interventions, max),
        ("max_cost_usd", metrics.cost_usd, expectations.max_cost_usd, max),
        ("max_wall_seconds", metrics.wall_seconds, expectations.max_wall_seconds, max),
        (
            "max_false_positives",
            metrics.false_positives,
            expectations.max_false_positives,
            max,
        ),
    )
    for name, actual, bound, direction in checks:
        if bound is None:
            continue
        violated = actual < bound if direction is min else actual > bound
        if violated:
            failures.append(f"{name}: actual={actual} bound={bound}")
    return tuple(failures)


def compare_reports(
    baseline: SuiteReport,
    current: SuiteReport,
    *,
    cost_tolerance: float = 0.2,
    wall_tolerance: float = 0.3,
) -> ComparisonResult:
    baseline_by_id = {result.scenario_id: result for result in baseline.results}
    current_by_id = {result.scenario_id: result for result in current.results}
    scenario_ids = list(dict.fromkeys([*baseline_by_id, *current_by_id]))
    comparisons: list[ScenarioComparison] = []
    regressions: list[str] = []
    for scenario_id in scenario_ids:
        base = baseline_by_id.get(scenario_id)
        curr = current_by_id.get(scenario_id)
        scenario_regressions: list[str] = []
        if base is None or curr is None:
            missing = "baseline" if base is None else "current"
            scenario_regressions.append(f"scenario missing from {missing}")
        elif base.metrics is not None and curr.metrics is not None:
            bm = base.metrics
            cm = curr.metrics
            if bm.solved and not cm.solved:
                scenario_regressions.append("solve regressed: true -> false")
            for name, before, after in (
                ("invalid_actions", bm.invalid_actions, cm.invalid_actions),
                ("false_positives", bm.false_positives, cm.false_positives),
                ("interventions", bm.interventions, cm.interventions),
            ):
                if after > before:
                    scenario_regressions.append(f"{name} increased: {before} -> {after}")
            if bm.cost_usd > 0 and cm.cost_usd > bm.cost_usd * (1 + cost_tolerance):
                scenario_regressions.append(
                    f"cost_usd increased beyond {cost_tolerance:.0%}: "
                    f"{bm.cost_usd} -> {cm.cost_usd}"
                )
            if bm.wall_seconds > 0 and cm.wall_seconds > bm.wall_seconds * (
                1 + wall_tolerance
            ):
                scenario_regressions.append(
                    f"wall_seconds increased beyond {wall_tolerance:.0%}: "
                    f"{bm.wall_seconds} -> {cm.wall_seconds}"
                )
        if curr is not None and curr.status is EvalScenarioStatus.ERROR:
            scenario_regressions.append(f"current run errored: {curr.error}")
        comparisons.append(
            ScenarioComparison(
                scenario_id=scenario_id,
                baseline=base.metrics if base is not None else None,
                current=curr.metrics if curr is not None else None,
                regressions=tuple(scenario_regressions),
            )
        )
        regressions.extend(f"{scenario_id}: {item}" for item in scenario_regressions)
    return ComparisonResult(
        baseline_suite=f"{baseline.suite}@{baseline.suite_version}",
        current_suite=f"{current.suite}@{current.suite_version}",
        scenarios=comparisons,
        regressions=tuple(regressions),
    )


def build_aggregate(results: list[ScenarioResult]) -> SuiteAggregate:
    metrics = [result.metrics for result in results if result.metrics is not None]
    solved = sum(1 for item in metrics if item.solved)
    rates = [item.validation_rate for item in metrics if item.validation_rate is not None]
    return SuiteAggregate(
        solve_rate=round(solved / len(results), 4) if results else 0.0,
        validation_rate=round(sum(rates) / len(rates), 4) if rates else None,
        passed=sum(
            1 for result in results if result.status is EvalScenarioStatus.PASSED
        ),
        failed=sum(
            1 for result in results if result.status is EvalScenarioStatus.FAILED
        ),
        errored=sum(
            1 for result in results if result.status is EvalScenarioStatus.ERROR
        ),
        total_cost_usd=round(sum(item.cost_usd for item in metrics), 6),
        total_tokens=sum(item.prompt_tokens + item.completion_tokens for item in metrics),
        total_wall_seconds=round(sum(item.wall_seconds for item in metrics), 3),
        total_invalid_actions=sum(item.invalid_actions for item in metrics),
        total_interventions=sum(item.interventions for item in metrics),
        total_false_positives=sum(item.false_positives for item in metrics),
    )


def utc_now() -> datetime:
    return datetime.now(UTC)
