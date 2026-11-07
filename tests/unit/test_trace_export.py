"""Trace export: one JSON document per task, out of the services' own data."""

from __future__ import annotations

import json
from pathlib import Path

from samurai_ai.cli import _make_services
from samurai_ai.domain.kernel import PlanTask, RiskClass
from samurai_ai.domain.operations import OperationMode, ScopeContract
from samurai_ai.services.trace_export import export_task_trace


def test_trace_collects_runs_artifacts_findings_events(tmp_path: Path) -> None:
    services = _make_services(tmp_path / "t.db")
    operations, kernel, execution = services[0], services[1], services[4]
    operation = operations.create(
        "Trace lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    kernel.create_plan(
        operation.id,
        "Plan",
        [PlanTask(id="t-1", title="Recon", risk_class=RiskClass.R1)],
        [],
    )

    # a run bound to the task, an artifact bound to it, and an event naming it
    from samurai_ai.domain.execution import ToolRun
    from samurai_ai.domain.operations import OperationEvent

    run = ToolRun(operation_id=operation.id, task_id="t-1", tool_id="shell.exec", tool_version=1)
    execution.runs.add_run(
        run,
        OperationEvent(
            operation_id=operation.id,
            event_type="tool.run.queued",
            payload={"run_id": run.id, "task_id": "t-1"},
        ),
    )
    from samurai_ai.domain.kernel import ArtifactKind

    kernel.register_artifact(
        operation.id, ArtifactKind.COMMAND_OUTPUT, "echo hi", summary="hi", task_id="t-1"
    )
    # a run of another task must not leak in
    other = ToolRun(operation_id=operation.id, task_id="t-2", tool_id="shell.exec", tool_version=1)
    execution.runs.add_run(
        other,
        OperationEvent(
            operation_id=operation.id, event_type="tool.run.queued",
            payload={"run_id": other.id, "task_id": "t-2"},
        ),
    )

    path = export_task_trace(operations, kernel, execution, operation.id, "t-1",
                             out_dir=tmp_path / "traces")
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["task"]["id"] == "t-1"
    assert [r["id"] for r in document["tool_runs"]] == [run.id]
    assert len(document["artifacts"]) == 1
    assert any(e["payload"].get("task_id") == "t-1" for e in document["events"])
    assert all(e["payload"].get("task_id") != "t-2" for e in document["events"])
    assert path.parent.name == operation.id


def test_trace_for_a_taskless_plan_still_writes(tmp_path: Path) -> None:
    services = _make_services(tmp_path / "t.db")
    operations, kernel, execution = services[0], services[1], services[4]
    operation = operations.create(
        "Empty lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("*.example.com",))
    )
    path = export_task_trace(operations, kernel, execution, operation.id, "t-9",
                             out_dir=tmp_path / "traces")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["tool_runs"] == [] and document["task"]["id"] == "t-9"
