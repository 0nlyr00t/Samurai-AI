import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from samurai_ai.domain.eval import (
    ComparisonResult,
    EvalExpectations,
    EvalLiveTarget,
    EvalScenario,
    EvalScenarioStatus,
    EvalSuite,
    ScenarioMetrics,
    ScenarioResult,
    SuiteAggregate,
    SuiteReport,
    build_aggregate,
    check_expectations,
    compare_reports,
    merge_metrics,
    metrics_from_events,
)
from samurai_ai.domain.kernel import OperationPlan, PlanTask
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.eval import EvalService, _model_of


def _event(event_type: str, payload: dict | None = None) -> OperationEvent:
    return OperationEvent(
        operation_id="op1", event_type=event_type, payload=payload or {}
    )


def _plan(*statuses: str) -> OperationPlan:
    return OperationPlan(
        operation_id="op1",
        title="plan",
        tasks=tuple(
            PlanTask(id=f"task-{index}", title=f"Task {index}", status=status)
            for index, status in enumerate(statuses)
        ),
    )


def _metrics(**overrides: object) -> ScenarioMetrics:
    base: dict = {
        "solved": True,
        "tasks_done": 2,
        "tasks_total": 2,
        "validation_rate": None,
        "findings_created": 0,
        "model_calls": 4,
        "prompt_tokens": 160,
        "completion_tokens": 32,
        "cost_usd": 0.000224,
        "wall_seconds": 1.0,
        "interventions": 1,
        "invalid_actions": 0,
        "false_positives": 0,
    }
    base.update(overrides)
    return ScenarioMetrics(**base)


def _report(
    name: str, results: list[ScenarioResult], *, version: str = "1.0.0"
) -> SuiteReport:
    now = datetime.now(UTC)
    return SuiteReport(
        suite=name,
        suite_version=version,
        samurai_version="0.1.0",
        config={},
        started_at=now,
        finished_at=now,
        results=results,
        aggregate=build_aggregate(results),
    )


def _result(scenario_id: str, metrics: ScenarioMetrics | None) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        status=EvalScenarioStatus.PASSED,
        metrics=metrics,
    )


def test_suite_requires_unique_scenario_ids() -> None:
    scenario = {
        "id": "one",
        "title": "One",
        "scope_includes": ["example.com"],
        "tasks": [{"id": "t1", "title": "T1"}],
        "script": {"responses": ["{}"]},
    }
    with pytest.raises(ValidationError):
        EvalSuite.model_validate(
            {"name": "s", "version": "1", "scenarios": [scenario, scenario]}
        )


def test_scenario_requires_unique_task_ids() -> None:
    with pytest.raises(ValidationError):
        EvalScenario.model_validate(
            {
                "id": "one",
                "title": "One",
                "scope_includes": ["example.com"],
                "tasks": [
                    {"id": "t1", "title": "T1"},
                    {"id": "t1", "title": "T1 again"},
                ],
                "script": {"responses": ["{}"]},
            }
        )


def test_a_scenario_needs_exactly_one_provider() -> None:
    base = {
        "id": "one",
        "title": "One",
        "scope_includes": ["example.com"],
        "tasks": [{"id": "t1", "title": "T1"}],
    }
    live = {"base_url": "http://x", "model": "local/m@v1", "secret_env": "KEY"}
    with pytest.raises(ValidationError):
        EvalScenario.model_validate(base)
    with pytest.raises(ValidationError):
        EvalScenario.model_validate({**base, "script": {"responses": ["{}"]}, "live": live})
    scenario = EvalScenario.model_validate({**base, "live": live})
    assert scenario.live is not None
    assert scenario.live.api_family.value == "openai-compatible"


def test_a_live_scenario_sets_the_model_not_an_id() -> None:
    live = {"base_url": "http://x", "model": "local/m@v1", "secret_env": "KEY"}
    base = {
        "id": "one",
        "title": "One",
        "scope_includes": ["example.com"],
        "tasks": [{"id": "t1", "title": "T1"}],
        "live": live,
    }
    with pytest.raises(ValidationError):
        EvalScenario.model_validate({**base, "model_id": "eval/other@v1"})
    assert _model_of(EvalScenario.model_validate(base)) == "local/m@v1"


def test_a_live_scenario_without_its_secret_fails_before_the_run(tmp_path: Path) -> None:
    suite = EvalSuite.model_validate(
        {
            "name": "live",
            "version": "1",
            "scenarios": [
                {
                    "id": "one",
                    "title": "One",
                    "scope_includes": ["example.com"],
                    "tasks": [{"id": "t1", "title": "T1"}],
                    "live": {
                        "base_url": "http://127.0.0.1:9",
                        "model": "local/m@v1",
                        "secret_env": "SAMURAI_EVAL_TEST_KEY",
                    },
                }
            ],
        }
    )
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SAMURAI_EVAL_TEST_KEY", None)
        report = EvalService().run_suite(suite, workdir=tmp_path)

    result = report.results[0]
    assert result.status is EvalScenarioStatus.ERROR
    assert "SAMURAI_EVAL_TEST_KEY" in (result.error or "")


def test_metrics_from_events_counts_all_signal_types() -> None:
    events = [
        _event("model.call", {"prompt_tokens": 10, "completion_tokens": 5,
                              "estimated_cost_usd": 0.001}),
        _event("model.call", {"prompt_tokens": 20, "completion_tokens": 7,
                              "estimated_cost_usd": 0.002}),
        _event("agent.command.rejected", {"reason": "x"}),
        _event("approval.requested", {}),
        _event("finding.created", {}),
        _event("finding.validated", {}),
        _event("finding.reported", {}),
        _event("finding.marked-false-positive", {}),
    ]
    metrics = metrics_from_events(events, _plan("done", "done"), wall_seconds=1.5)
    assert metrics.solved is True
    assert metrics.tasks_done == 2 and metrics.tasks_total == 2
    assert metrics.model_calls == 2
    assert metrics.prompt_tokens == 30
    assert metrics.completion_tokens == 12
    assert metrics.cost_usd == pytest.approx(0.003)
    assert metrics.invalid_actions == 1
    assert metrics.interventions == 1
    assert metrics.findings_created == 1
    assert metrics.false_positives == 1
    assert metrics.validation_rate == pytest.approx(1.0)
    assert metrics.wall_seconds == pytest.approx(1.5)


def test_metrics_unsolved_when_task_pending_and_rate_none_without_reports() -> None:
    metrics = metrics_from_events([_event("finding.created", {})],
                                  _plan("done", "pending"), wall_seconds=0.1)
    assert metrics.solved is False
    assert metrics.validation_rate is None


def test_check_expectations_reports_each_violation() -> None:
    metrics = _metrics(solved=False, tasks_done=0, invalid_actions=2, cost_usd=5.0)
    failures = check_expectations(
        metrics,
        EvalExpectations(
            expect_solve=True, min_tasks_done=1, max_invalid_actions=1,
            max_cost_usd=1.0,
        ),
    )
    assert len(failures) == 4
    assert any("expect_solve" in failure for failure in failures)
    assert any("min_tasks_done" in failure for failure in failures)
    assert any("max_invalid_actions" in failure for failure in failures)
    assert any("max_cost_usd" in failure for failure in failures)


def test_merge_metrics_takes_median_and_all_solved() -> None:
    merged = merge_metrics([
        _metrics(wall_seconds=1.0, model_calls=2),
        _metrics(wall_seconds=3.0, model_calls=4),
        _metrics(wall_seconds=2.0, model_calls=3),
    ])
    assert merged.wall_seconds == pytest.approx(2.0)
    assert merged.model_calls == 3
    unsolved = merge_metrics([_metrics(), _metrics(solved=False)])
    assert unsolved.solved is False


def test_compare_reports_detects_solve_and_growth_regressions() -> None:
    baseline = _report("s", [_result("a", _metrics())])
    current = _report(
        "s",
        [_result("a", _metrics(solved=False, invalid_actions=3,
                               cost_usd=0.001, wall_seconds=5.0))],
    )
    comparison = compare_reports(baseline, current)
    assert isinstance(comparison, ComparisonResult)
    assert not comparison.passed
    assert any("solve regressed" in item for item in comparison.regressions)
    assert any("invalid_actions increased" in item for item in comparison.regressions)
    assert any("cost_usd increased" in item for item in comparison.regressions)
    assert any("wall_seconds increased" in item for item in comparison.regressions)


def test_compare_reports_passes_within_tolerance() -> None:
    baseline = _report("s", [_result("a", _metrics(cost_usd=1.0, wall_seconds=10.0))])
    current = _report(
        "s", [_result("a", _metrics(cost_usd=1.1, wall_seconds=12.0))]
    )
    assert compare_reports(baseline, current).passed


def test_compare_reports_wall_floor_ignores_subsecond_noise() -> None:
    baseline = _report("s", [_result("a", _metrics(wall_seconds=0.03))])
    current = _report("s", [_result("a", _metrics(wall_seconds=0.05))])
    assert compare_reports(baseline, current).passed
    noisy = compare_reports(baseline, current, min_wall_seconds=0.0)
    assert not noisy.passed


def test_compare_reports_flags_missing_scenario_and_error_run() -> None:
    baseline = _report("s", [_result("a", _metrics()), _result("b", _metrics())])
    current = _report(
        "s",
        [
            _result("a", _metrics()),
            ScenarioResult(
                scenario_id="b", status=EvalScenarioStatus.ERROR, error="boom"
            ),
        ],
    )
    comparison = compare_reports(baseline, current)
    assert not comparison.passed
    assert any("current run errored" in item for item in comparison.regressions)
    missing = compare_reports(
        baseline, _report("s", [_result("a", _metrics())])
    )
    assert any("missing from current" in item for item in missing.regressions)


def test_build_aggregate_rates_and_totals() -> None:
    results = [
        _result("a", _metrics(validation_rate=1.0, cost_usd=0.5)),
        ScenarioResult(
            scenario_id="b",
            status=EvalScenarioStatus.FAILED,
            metrics=_metrics(solved=False, validation_rate=0.5, cost_usd=0.25),
            expectation_failures=("x",),
        ),
        ScenarioResult(
            scenario_id="c", status=EvalScenarioStatus.ERROR, error="boom"
        ),
    ]
    aggregate = build_aggregate(results)
    assert isinstance(aggregate, SuiteAggregate)
    assert aggregate.passed == 1 and aggregate.failed == 1 and aggregate.errored == 1
    assert aggregate.solve_rate == pytest.approx(1 / 3, rel=1e-3)
    assert aggregate.validation_rate == pytest.approx(0.75)
    assert aggregate.total_cost_usd == pytest.approx(0.75)


def test_a_live_target_can_turn_off_tls_verification() -> None:
    live = {
        "base_url": "https://gateway.example/v1",
        "model": "gateway/m@v1",
        "secret_env": "KEY",
    }
    base = {
        "id": "tls",
        "title": "TLS",
        "scope_includes": ["example.com"],
        "tasks": [{"id": "t1", "title": "T1"}],
    }
    assert EvalLiveTarget.model_validate(live).verify_tls is True
    scenario = EvalScenario.model_validate(
        {**base, "live": {**live, "verify_tls": False}}
    )
    assert scenario.live is not None
    assert scenario.live.verify_tls is False


def test_the_live_stack_carries_verify_tls_into_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from samurai_ai.services.eval import _ScenarioStack

    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    stack = _ScenarioStack(
        tmp_path / "tls",
        "https://gateway.example/v1",
        "gateway/m@v1",
        api_key="k",
        verify_tls=False,
    )
    provider = stack.models.registry.get_provider("gateway")
    assert provider.verify_tls is False
