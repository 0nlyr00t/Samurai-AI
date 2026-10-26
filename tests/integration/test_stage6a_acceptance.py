"""Stage 6a acceptance: reproducible evaluation reports and regression detection.

A fixed suite run twice against the scripted provider must produce identical
volume metrics (calls, tokens, cost, solved, invalid actions) — only wall time
may drift. The same suite must pass `eval compare` against itself and fail
against a degraded report, which is the historical-regression gate from
development plan §8/§10.
"""

import json
from pathlib import Path

import pytest

from samurai_ai.cli import main
from samurai_ai.domain.eval import compare_reports
from samurai_ai.services.eval import EvalService, load_suite

SUITE_PATH = Path(__file__).resolve().parents[2] / "evals" / "smoke.json"

STABLE_FIELDS = (
    "solved",
    "tasks_done",
    "tasks_total",
    "model_calls",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "interventions",
    "invalid_actions",
    "false_positives",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    return tmp_path


def test_fixed_config_produces_reproducible_metrics(tmp_path: Path) -> None:
    suite = load_suite(SUITE_PATH)
    service = EvalService()
    first = service.run_suite(suite, workdir=tmp_path / "first")
    second = service.run_suite(suite, workdir=tmp_path / "second")

    assert first.passed, first.results
    assert second.passed, second.results
    assert first.samurai_version == second.samurai_version
    for a, b in zip(first.results, second.results, strict=True):
        assert a.metrics is not None and b.metrics is not None
        for field in STABLE_FIELDS:
            assert getattr(a.metrics, field) == getattr(b.metrics, field), field

    aggregate = first.aggregate
    assert aggregate.solve_rate == 1.0
    assert aggregate.total_invalid_actions == 1  # rejecting-executor by design
    assert aggregate.total_cost_usd > 0

    comparison = compare_reports(first, second)
    assert comparison.passed, comparison.regressions


def test_compare_gates_regression(home: Path, capsys) -> None:
    baseline = home / "baseline.json"
    assert main(["eval", "run", str(SUITE_PATH), "--out", str(baseline)]) == 0
    capsys.readouterr()

    degraded = json.loads(baseline.read_text(encoding="utf-8"))
    degraded["results"][0]["metrics"]["cost_usd"] *= 10
    current = home / "current.json"
    current.write_text(json.dumps(degraded), encoding="utf-8")

    assert main(["eval", "compare", str(baseline), str(current)]) == 1
    assert "cost_usd increased" in capsys.readouterr().err
