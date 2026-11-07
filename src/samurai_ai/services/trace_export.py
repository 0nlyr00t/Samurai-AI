"""Export a task's trace: every run, artifact, finding and event it touched.

The operator's question "what exactly happened on this task" deserves a file,
not a scroll through the journal. The trace is a JSON document per task: the
plan entry, the tool runs bound to it, the artifacts they produced, the
findings those artifacts back, and the operation events that name the task.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from samurai_ai.services.execution import ToolExecutionService
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.operations import OperationService


def default_traces_dir() -> Path:
    return Path.home() / ".config" / "samurai" / "traces"


def export_task_trace(
    operations: OperationService,
    kernel: KernelService,
    execution: ToolExecutionService,
    operation_id: str,
    task_id: str,
    out_dir: Path | None = None,
) -> Path:
    """Write the task trace as JSON and return its path."""
    operation = operations.get(operation_id)
    plan = kernel.get_current_plan(operation_id)
    task = next((t for t in plan.tasks if t.id == task_id), None) if plan else None

    runs = [r for r in execution.list_runs(operation_id) if r.task_id == task_id]
    artifacts = [a for a in kernel.kernel.list_artifacts(operation_id) if a.task_id == task_id]
    artifact_ids = {a.id for a in artifacts}
    findings = [
        f
        for f in kernel.kernel.list_findings(operation_id)
        if artifact_ids & set(f.evidence_ids)
    ]
    events = [
        e
        for e in operations.events(operation_id)
        if task_id in json.dumps(e.payload, ensure_ascii=False)
    ]

    document = {
        "operation": {"id": operation.id, "name": operation.name, "mode": operation.mode.value},
        "task": (
            {
                "id": task.id,
                "title": task.title,
                "status": task.status.value,
                "risk_class": task.risk_class.value,
            }
            if task
            else {"id": task_id, "title": None}
        ),
        "tool_runs": [json.loads(r.model_dump_json()) for r in runs],
        "artifacts": [json.loads(a.model_dump_json()) for a in artifacts],
        "findings": [json.loads(f.model_dump_json()) for f in findings],
        "events": [json.loads(e.model_dump_json()) for e in events],
        "exported_at": datetime.now(UTC).isoformat(),
    }

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = (out_dir or default_traces_dir()) / operation_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{task_id}-{stamp}.json"
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
