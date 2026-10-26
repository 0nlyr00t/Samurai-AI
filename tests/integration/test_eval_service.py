import json
from pathlib import Path

import pytest

from samurai_ai.domain.eval import (
    EvalScenarioStatus,
    EvalSuite,
    compare_reports,
)
from samurai_ai.services.eval import (
    EvalService,
    load_report,
    load_suite,
    report_to_json,
)

PLANNER = '{"steps": ["probe it"]}'
EXECUTOR = (
    '{"commands": [{"type": "note", "key": "done", "value": "yes"},'
    ' {"type": "complete_task"}]}'
)


def _suite_dict() -> dict:
    return {
        "name": "mini",
        "version": "1.0.0",
        "scenarios": [
            {
                "id": "solo",
                "title": "Single task",
                "scope_includes": ["example.com"],
                "tasks": [{"id": "recon", "title": "Recon"}],
                "script": {"responses": [PLANNER, EXECUTOR]},
                "expectations": {"max_invalid_actions": 0},
            },
            {
                "id": "pair",
                "title": "Two tasks in a swarm",
                "scope_includes": ["example.com"],
                "tasks": [
                    {"id": "one", "title": "One"},
                    {"id": "two", "title": "Two"},
                ],
                "workers": 2,
                "script": {"responses": [PLANNER, PLANNER, EXECUTOR, EXECUTOR]},
                "expectations": {"max_invalid_actions": 0, "max_interventions": 1},
            },
        ],
    }


def test_run_suite_is_deterministic_on_cost_tokens_calls(tmp_path: Path) -> None:
    suite = EvalSuite.model_validate(_suite_dict())
    service = EvalService()
    first = service.run_suite(suite, workdir=tmp_path / "first")
    second = service.run_suite(suite, workdir=tmp_path / "second")

    assert first.passed and second.passed
    assert len(first.results) == 2
    for a, b in zip(first.results, second.results, strict=True):
        assert a.scenario_id == b.scenario_id
        assert a.metrics is not None and b.metrics is not None
        assert a.metrics.model_calls == b.metrics.model_calls
        assert a.metrics.prompt_tokens == b.metrics.prompt_tokens
        assert a.metrics.completion_tokens == b.metrics.completion_tokens
        assert a.metrics.cost_usd == b.metrics.cost_usd
        assert a.metrics.solved == b.metrics.solved

    solo = first.results[0].metrics
    assert solo is not None
    assert solo.model_calls == 2
    assert solo.interventions == 1
    assert solo.invalid_actions == 0
    assert solo.cost_usd > 0


def test_run_suite_marks_expectation_failures(tmp_path: Path) -> None:
    raw = _suite_dict()
    raw["scenarios"][0]["expectations"] = {"max_invalid_actions": 0,
                                           "min_tasks_done": 2}
    suite = EvalSuite.model_validate(raw)
    report = EvalService().run_suite(suite, workdir=tmp_path)
    assert not report.passed
    solo = report.results[0]
    assert solo.status is EvalScenarioStatus.FAILED
    assert any("min_tasks_done" in item for item in solo.expectation_failures)


def test_repeats_merge_by_median(tmp_path: Path) -> None:
    suite = EvalSuite.model_validate(_suite_dict())
    report = EvalService().run_suite(suite, workdir=tmp_path, repeats=3)
    assert report.config["repeats"] == 3
    solo = report.results[0].metrics
    assert solo is not None and solo.model_calls == 2


def test_report_roundtrip_and_compare_detects_regression(tmp_path: Path) -> None:
    suite = EvalSuite.model_validate(_suite_dict())
    service = EvalService()
    baseline = service.run_suite(suite, workdir=tmp_path / "base")

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(report_to_json(baseline), encoding="utf-8")
    restored = load_report(baseline_path)
    assert restored.passed
    assert restored.aggregate.solve_rate == baseline.aggregate.solve_rate

    degraded = json.loads(report_to_json(baseline))
    degraded["results"][0]["metrics"]["solved"] = False
    degraded["results"][0]["metrics"]["invalid_actions"] = 5
    current_path = tmp_path / "current.json"
    current_path.write_text(json.dumps(degraded), encoding="utf-8")
    comparison = compare_reports(load_report(baseline_path), load_report(current_path))
    assert not comparison.passed
    assert any("solve regressed" in item for item in comparison.regressions)
    assert any("invalid_actions increased" in item for item in comparison.regressions)


def test_load_suite_rejects_bad_files(tmp_path: Path) -> None:
    from samurai_ai.domain.eval import EvalSuiteError

    missing = tmp_path / "missing.json"
    with pytest.raises(EvalSuiteError):
        load_suite(missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(EvalSuiteError):
        load_suite(bad)
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(EvalSuiteError):
        load_suite(invalid)
