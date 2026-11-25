"""A subagent hands back store records with ids; its notes stay labelled."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.execution import ToolRun, ToolRunStatus
from samurai_ai.domain.kernel import (
    Artifact,
    ArtifactKind,
    Finding,
    FindingSeverity,
    TaskStatus,
)
from samurai_ai.domain.subagent_report import MAX_RUNS, build_report

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _run() -> AgentRun:
    run = AgentRun(operation_id="op", task_id="t1", created_at=T0)
    run.finish(AgentRunStatus.COMPLETED)
    return run


def _tool_run(index: int, task: str = "t1", at: datetime | None = None) -> ToolRun:
    return ToolRun(
        id=f"run_{index}",
        operation_id="op",
        task_id=task,
        tool_id="shell.exec",
        tool_version=1,
        status=ToolRunStatus.COMPLETED,
        exit_code=0,
        created_at=at or T0 + timedelta(seconds=index + 1),
    )


def _artifact(run_id: str) -> Artifact:
    return Artifact(
        id=f"art_{run_id}",
        operation_id="op",
        kind=ArtifactKind.COMMAND_OUTPUT,
        sha256="0" * 64,
        size_bytes=1,
        path="/x",
        created_by="tool:shell.exec",
        source_run_id=run_id,
    )


def test_report_lists_only_this_runs_records() -> None:
    run = _run()
    run.finished_at = T0 + timedelta(hours=1)
    finding = Finding(
        id="find_1",
        operation_id="op",
        title="open redirect",
        severity=FindingSeverity.MEDIUM,
        evidence_ids=("art_run_1",),
    )
    report = build_report(
        run,
        task_status=TaskStatus.DONE,
        tool_runs=[
            _tool_run(1),
            _tool_run(2, task="other"),
            _tool_run(3, at=T0 - timedelta(minutes=1)),
        ],
        findings={"find_1": finding},
        artifacts=[_artifact("run_1"), _artifact("run_2")],
        events=[
            ("agent.step", {"run_id": run.id, "step": "finding-proposed", "finding_id": "find_1"}),
            ("agent.step", {"run_id": "other", "step": "finding-proposed", "finding_id": "x"}),
            ("agent.command.rejected", {"run_id": run.id}),
            ("agent.run.finish", {"run_id": run.id, "status": "completed"}),
        ],
        notes={
            f"run.{run.id}.tool-result.shell.exec": "raw excerpt",
            f"run.{run.id}.found": "port 443 serves an admin panel",
        },
    )
    assert report.proven
    records = report.records()
    assert [r["id"] for r in records["tool_runs"]] == ["run_1"]
    assert records["findings"][0]["evidence_ids"] == ["art_run_1"]
    assert [a["id"] for a in records["artifacts"]] == ["art_run_1"]
    assert records["rejected_commands"] == 1
    assert report.notes == (f"run.{run.id}.found: port 443 serves an admin panel",)

    rendered = report.render()
    payload = json.loads(rendered.splitlines()[1])
    assert payload["run_id"] == run.id
    assert "unverified" in rendered
    assert "did not close with evidence" not in rendered


def test_unclosed_task_is_flagged_as_a_lead() -> None:
    run = AgentRun(operation_id="op", task_id="t1", created_at=T0)
    run.finish(AgentRunStatus.FAILED)
    report = build_report(
        run,
        task_status=TaskStatus.BLOCKED,
        tool_runs=[],
        findings={},
        artifacts=[],
        events=[("agent.run.finish", {"run_id": run.id, "reason": "step budget spent"})],
        notes={f"run.{run.id}.guess": "maybe sqli"},
    )
    assert not report.proven
    assert report.records()["reason"] == "step budget spent"
    assert "did not close with evidence" in report.render()


def test_many_runs_collapse_into_a_count() -> None:
    run = AgentRun(operation_id="op", task_id="t1", created_at=T0)
    report = build_report(
        run,
        task_status=None,
        tool_runs=[_tool_run(index) for index in range(MAX_RUNS + 5)],
        findings={},
        artifacts=[],
        events=[],
        notes={},
    )
    records = report.records()
    assert len(records["tool_runs"]) == MAX_RUNS
    assert records["tool_runs_omitted"] == 5
    assert records["task_status"] == "missing"
    assert records["tool_runs"][-1]["id"] == f"run_{MAX_RUNS + 4}"
