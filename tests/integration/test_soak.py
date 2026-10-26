"""Soak/performance: repeated scripted runs must not degrade or leak.

Ten consecutive scenario executions through the eval harness must produce
identical volume metrics, bounded wall-time drift, no leaked server threads
and a linearly-growing (not duplicated) journal.
"""

import statistics
import threading
import time
from pathlib import Path

from samurai_ai.domain.eval import EvalSuite
from samurai_ai.services.eval import EvalService
from samurai_ai.storage.database import Database

PLANNER = '{"steps": ["probe it"]}'
EXECUTOR = (
    '{"commands": [{"type": "note", "key": "done", "value": "yes"},'
    ' {"type": "complete_task"}]}'
)

REPEATS = 10


def _suite() -> EvalSuite:
    return EvalSuite.model_validate(
        {
            "name": "soak",
            "version": "1.0.0",
            "scenarios": [
                {
                    "id": "loop",
                    "title": "Single task loop",
                    "scope_includes": ["example.com"],
                    "tasks": [{"id": "recon", "title": "Recon"}],
                    "script": {"responses": [PLANNER, EXECUTOR]},
                }
            ],
        }
    )


def _event_count(scenario_dir: Path) -> int:
    database = Database(scenario_dir / "samurai.db")
    with database.connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS n FROM operation_events").fetchone()
    return int(row["n"])


def test_repeated_runs_stay_stable(tmp_path: Path) -> None:
    suite = _suite()
    service = EvalService()
    threads_before = len(threading.enumerate())

    walls: list[float] = []
    event_counts: list[int] = []
    for attempt in range(REPEATS):
        started = time.monotonic()
        report = service.run_suite(suite, workdir=tmp_path / f"run{attempt}")
        walls.append(time.monotonic() - started)
        result = report.results[0]
        assert result.metrics is not None
        assert result.metrics.solved is True
        assert result.metrics.model_calls == 2
        assert result.metrics.invalid_actions == 0
        event_counts.append(_event_count(tmp_path / f"run{attempt}" / "soak-loop-0"))

    # every run writes the same journal shape — no duplicated or lost events
    assert len(set(event_counts)) == 1
    # no wall-time degradation trend: the last runs stay near the median
    median_wall = statistics.median(walls)
    assert max(walls[-3:]) < median_wall * 3 + 0.5
    # scripted servers shut down cleanly — no thread accumulation
    assert len(threading.enumerate()) <= threads_before + 1


def test_suite_level_repeats_merge_stays_deterministic(tmp_path: Path) -> None:
    suite = _suite()
    report = EvalService().run_suite(suite, workdir=tmp_path, repeats=3)
    metrics = report.results[0].metrics
    assert metrics is not None
    assert metrics.model_calls == 2
    assert metrics.cost_usd > 0
    # scenario dirs exist per repeat
    dirs = sorted(tmp_path.glob("soak-loop-*"))
    assert len(dirs) == 3
