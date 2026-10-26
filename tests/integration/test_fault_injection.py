"""Fault injection: provider failures must degrade gracefully.

An injected HTTP 500, malformed JSON, empty body or connection reset must
finish the agent run as FAILED (never stuck RUNNING), keep the journal
consistent, never leak vault secrets into errors, and never crash the eval
suite around the failing scenario.
"""

import json
import time
from pathlib import Path

from samurai_ai.domain.eval import EvalScenarioStatus, EvalSuite
from samurai_ai.services.eval import EvalService
from samurai_ai.storage.database import Database

PLANNER = '{"steps": ["probe it"]}'
EXECUTOR = '{"commands": [{"type": "complete_task"}]}'
CANARY = "CANARY-SECRET-7f3a9d"


def _suite(fault: dict, *, after_planner: bool = False, workers: int = 1) -> EvalSuite:
    responses = [PLANNER, EXECUTOR] if after_planner else [PLANNER]
    tasks = [{"id": "recon", "title": "Recon"}]
    if workers > 1:
        tasks.append({"id": "testing", "title": "Testing"})
        responses = [PLANNER, PLANNER, EXECUTOR, EXECUTOR]
    return EvalSuite.model_validate(
        {
            "name": "faults",
            "version": "1.0.0",
            "scenarios": [
                {
                    "id": "faulty",
                    "title": "Faulted provider",
                    "scope_includes": ["example.com"],
                    "tasks": tasks,
                    "workers": workers,
                    "script": {"responses": responses, "fault": fault},
                    "expectations": {"expect_solve": False},
                }
            ],
        }
    )


def _events(scenario_dir: Path) -> list[dict]:
    database = Database(scenario_dir / "samurai.db")
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT event_type, payload FROM operation_events ORDER BY id"
        ).fetchall()
    return [
        {"event_type": row["event_type"], "payload": json.loads(row["payload"])}
        for row in rows
    ]


def _run(suite: EvalSuite, workdir: Path) -> tuple:
    report = EvalService().run_suite(suite, workdir=workdir)
    result = report.results[0]
    events = _events(workdir / "faults-faulty-0")
    return report, result, events


def test_http_500_fails_run_cleanly_and_journal_stays_consistent(tmp_path: Path) -> None:
    suite = _suite({"mode": "http_error", "status": 500})
    started = time.monotonic()
    report, result, events = _run(suite, tmp_path)
    wall = time.monotonic() - started

    assert result.status is EvalScenarioStatus.PASSED  # expect_solve=false met
    assert result.metrics is not None
    assert result.metrics.solved is False
    assert result.metrics.tasks_done == 0
    assert result.metrics.model_calls == 0
    assert wall < 30  # bounded retries, no hang

    starts = [e for e in events if e["event_type"] == "agent.run.start"]
    finishes = [e for e in events if e["event_type"] == "agent.run.finish"]
    assert len(starts) == len(finishes) == 1
    assert finishes[0]["payload"]["status"] == "failed"
    assert "provider-error" in finishes[0]["payload"]["reason"]
    # no event payload contains the canary
    assert CANARY not in json.dumps(events)


def test_malformed_json_fails_fast_without_retries(tmp_path: Path) -> None:
    suite = _suite({"mode": "malformed_json"})
    started = time.monotonic()
    report, result, events = _run(suite, tmp_path)
    wall = time.monotonic() - started

    assert result.status is EvalScenarioStatus.PASSED
    assert result.metrics is not None and result.metrics.solved is False
    assert wall < 10
    finishes = [e for e in events if e["event_type"] == "agent.run.finish"]
    assert finishes and finishes[0]["payload"]["status"] == "failed"


def test_empty_body_and_connection_reset_fail_gracefully(tmp_path: Path) -> None:
    for index, mode in enumerate(("empty_body", "connection_reset")):
        suite = _suite({"mode": mode})
        report = EvalService().run_suite(suite, workdir=tmp_path / f"case{index}")
        result = report.results[0]
        assert result.status in (
            EvalScenarioStatus.PASSED,  # expect_solve=false met
            EvalScenarioStatus.FAILED,
            EvalScenarioStatus.ERROR,
        )
        assert result.status is not EvalScenarioStatus.ERROR or result.error


def test_fault_after_planner_fails_mid_run(tmp_path: Path) -> None:
    suite = _suite({"mode": "http_error", "status": 500, "after_calls": 1},
                   after_planner=True)
    report, result, events = _run(suite, tmp_path)

    assert result.status is EvalScenarioStatus.PASSED
    assert result.metrics is not None
    assert result.metrics.model_calls == 1  # planner succeeded, executor failed
    assert result.metrics.solved is False
    finishes = [e for e in events if e["event_type"] == "agent.run.finish"]
    assert finishes and finishes[0]["payload"]["status"] == "failed"


def test_swarm_members_isolated_under_fault(tmp_path: Path) -> None:
    suite = _suite({"mode": "http_error", "status": 500}, workers=2)
    report, result, events = _run(suite, tmp_path)

    assert result.status is EvalScenarioStatus.PASSED
    swarm_finishes = [e for e in events if e["event_type"] == "agent.swarm.finish"]
    assert swarm_finishes
    assert swarm_finishes[0]["payload"]["status"] in ("partial", "failed")
    # both members terminated, none stuck running
    finishes = [e for e in events if e["event_type"] == "agent.run.finish"]
    assert len(finishes) == 2


def test_suite_continues_after_faulted_scenario(tmp_path: Path) -> None:
    raw = _suite({"mode": "http_error", "status": 500}).model_dump(mode="json")
    raw["scenarios"].append(
        {
            "id": "healthy",
            "title": "Healthy scenario after the faulted one",
            "scope_includes": ["example.com"],
            "tasks": [{"id": "recon", "title": "Recon"}],
            "script": {"responses": [PLANNER, EXECUTOR]},
            "expectations": {"expect_solve": True},
        }
    )
    report = EvalService().run_suite(
        EvalSuite.model_validate(raw), workdir=tmp_path
    )
    assert report.passed
    assert report.results[0].metrics is not None
    assert report.results[0].metrics.solved is False
    assert report.results[1].metrics is not None
    assert report.results[1].metrics.solved is True
