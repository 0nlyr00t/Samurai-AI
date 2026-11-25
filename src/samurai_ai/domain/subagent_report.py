"""What a subagent hands back: records with ids, not prose.

The parent chat used to receive the subagent's blackboard notes — its own
account of what it did. That is a claim. The report below is assembled from the
store after the run ends: the task's real status, the tool runs it made, the
findings it filed and the evidence bound to them, each with an id the parent
can cite or check. The subagent's notes still ride along, labelled as
unverified, so nothing it learned is lost but nothing it said outranks a record.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from samurai_ai.domain.agents import AgentRun
from samurai_ai.domain.execution import ToolRun
from samurai_ai.domain.kernel import Artifact, Finding, TaskStatus

#: the parent reads the newest runs; older ones collapse into a count
MAX_RUNS = 12
MAX_FINDINGS = 8
MAX_ARTIFACTS = 12
MAX_NOTES = 6
NOTE_CHARS = 300
#: notes the orchestrator writes for itself (tool excerpts) are not the
#: subagent's conclusions and would only repeat the run list
_INTERNAL_NOTE_PREFIXES = ("tool-result.",)


@dataclass(frozen=True)
class SubagentReport:
    run_id: str
    task_id: str
    status: str
    task_status: str
    reason: str = ""
    runs: tuple[dict[str, Any], ...] = ()
    runs_omitted: int = 0
    findings: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    rejected_commands: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def proven(self) -> bool:
        """The task closed through the evidence gate (done is only reachable so)."""
        return self.task_status == TaskStatus.DONE.value

    def records(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": self.status,
            "task_status": self.task_status,
            "proven": self.proven,
            "tool_runs": list(self.runs),
            "findings": list(self.findings),
            "artifacts": list(self.artifacts),
        }
        if self.runs_omitted:
            data["tool_runs_omitted"] = self.runs_omitted
        if self.reason:
            data["reason"] = self.reason
        if self.rejected_commands:
            data["rejected_commands"] = self.rejected_commands
        return data

    def render(self) -> str:
        """Records as JSON first; the subagent's own words after, marked as such."""
        lines = [
            "records (from the operation store; cite these ids):",
            json.dumps(self.records(), ensure_ascii=False, sort_keys=True),
        ]
        if not self.proven:
            lines.append(
                "the task did not close with evidence: treat any claim below as a lead, "
                "not a result"
            )
        if self.notes:
            lines.append("subagent notes (unverified):")
            lines += [f"- {note}" for note in self.notes]
        return "\n".join(lines)


def _internal(key: str) -> bool:
    # keys are namespaced (``run.<id>.tool-result.x``); judge the bare name
    bare = key.split(".", 2)[-1] if key.startswith("run.") else key
    return bare.startswith(_INTERNAL_NOTE_PREFIXES)


def _within(run: AgentRun, tool_run: ToolRun) -> bool:
    if tool_run.task_id != run.task_id or tool_run.created_at < run.created_at:
        return False
    return run.finished_at is None or tool_run.created_at <= run.finished_at


def build_report(
    run: AgentRun,
    *,
    task_status: TaskStatus | None,
    tool_runs: Iterable[ToolRun],
    findings: Mapping[str, Finding],
    artifacts: Iterable[Artifact],
    events: Sequence[tuple[str, Mapping[str, Any]]],
    notes: Mapping[str, str],
) -> SubagentReport:
    """Assemble the report from what the store holds for this run.

    ``events`` are ``(event_type, payload)`` pairs of the operation journal;
    ``findings`` maps id → current record, so a finding shows its status now,
    not the one it was filed with.
    """
    own_runs = [tool_run for tool_run in tool_runs if _within(run, tool_run)]
    run_ids = {tool_run.id for tool_run in own_runs}
    shown = own_runs[-MAX_RUNS:]
    runs = tuple(
        {
            "id": tool_run.id,
            "tool": tool_run.tool_id,
            "status": tool_run.status.value,
            "exit_code": tool_run.exit_code,
        }
        for tool_run in shown
    )
    reason = ""
    rejected = 0
    finding_ids: list[str] = []
    for event_type, payload in events:
        if payload.get("run_id") != run.id:
            continue
        if event_type == "agent.command.rejected":
            rejected += 1
        elif event_type == "agent.run.finish":
            reason = str(payload.get("reason") or "")
        elif event_type == "agent.step" and payload.get("step") == "finding-proposed":
            finding_id = str(payload.get("finding_id") or "")
            if finding_id and finding_id not in finding_ids:
                finding_ids.append(finding_id)
    found: list[dict[str, Any]] = []
    for finding_id in finding_ids[-MAX_FINDINGS:]:
        finding = findings.get(finding_id)
        if finding is None:
            continue
        found.append(
            {
                "id": finding.id,
                "title": finding.title,
                "severity": finding.severity.value,
                "status": finding.status.value,
                "evidence_ids": list(finding.evidence_ids),
            }
        )
    bound = [
        {"id": artifact.id, "kind": artifact.kind.value, "run_id": artifact.source_run_id}
        for artifact in artifacts
        if artifact.source_run_id in run_ids
    ][-MAX_ARTIFACTS:]
    own_notes = [
        f"{key}: {value}"[:NOTE_CHARS]
        for key, value in notes.items()
        if not _internal(key)
    ][:MAX_NOTES]
    return SubagentReport(
        run_id=run.id,
        task_id=run.task_id,
        status=run.status.value,
        task_status=task_status.value if task_status is not None else "missing",
        reason=reason,
        runs=runs,
        runs_omitted=len(own_runs) - len(shown),
        findings=tuple(found),
        artifacts=tuple(bound),
        rejected_commands=rejected,
        notes=tuple(own_notes),
    )
