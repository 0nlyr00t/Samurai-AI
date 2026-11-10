"""Benchmark domain: a scenario carries its box class into the report."""

from __future__ import annotations

from datetime import UTC, datetime

from samurai_ai.domain.bench import (
    BenchDifficulty,
    BenchReport,
    BenchResult,
    BenchScenario,
    BenchVerdict,
)
from samurai_ai.services.bench import render_table


def _scenario(**overrides: object) -> BenchScenario:
    payload: dict[str, object] = {"id": "box", "title": "a box", "objective": "own the box"}
    payload.update(overrides)
    return BenchScenario.model_validate(payload)


def test_the_difficulty_defaults_to_medium_and_is_settable() -> None:
    assert _scenario().difficulty is BenchDifficulty.MEDIUM
    assert _scenario(difficulty="hard").difficulty is BenchDifficulty.HARD


def test_the_table_shows_the_class_so_runs_compare_like_with_like() -> None:
    now = datetime.now(UTC)
    report = BenchReport(
        suite="s",
        suite_version="1",
        samurai_version="0.0.0",
        started_at=now,
        finished_at=now,
        results=[
            BenchResult(
                scenario_id="box",
                difficulty="hard",
                verdict=BenchVerdict.ERROR,
                error="canary refused",
            )
        ],
    )
    text = render_table(report)
    assert "class" in text
    assert "hard" in text
    assert "canary refused" in text
