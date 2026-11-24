"""Evidence-based readiness of one operation, derived from its own records."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.execution import ToolRunStatus
from samurai_ai.domain.hypotheses import hypothesis_coverage
from samurai_ai.services.finalization import FinalizationService


class OperationReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    hypotheses_total: int
    hypotheses_closed: int
    open_ids: tuple[str, ...]
    testing_ids: tuple[str, ...]
    goal_reached: bool
    goal_state: str
    finalization_required: bool
    finalization_ready: bool
    eligible_fact_ids: tuple[str, ...]
    pending_execution_ids: tuple[str, ...]
    pending_approval_ids: tuple[str, ...]
    budget_steps_left: int
    budget_seconds_left: int
    domain_findings: int
    terminal_reason: str
    model_calls: int
    tool_runs: int
    failed_runs: int
    artifacts: int
    approvals_requested: int
    scope_refusals: int
    wall_seconds: float
    cost_usd: float

    @property
    def hypotheses_complete(self) -> bool:
        return not self.open_ids and not self.testing_ids

    @property
    def evidence_success(self) -> bool:
        return self.goal_reached and self.hypotheses_complete


class ReadinessService:
    def __init__(
        self,
        operations: Any,
        kernel: Any,
        execution: Any,
        ctf: Any,
        graph: Any,
        goals: Any,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.execution = execution
        self.ctf = ctf
        self.graph = graph
        self.goals = goals

    def report(self, operation_id: str) -> OperationReadiness:
        events = list(self.operations.events(operation_id))
        coverage = hypothesis_coverage(
            self.graph.for_operation(operation_id), self.ctf.list_hypotheses(operation_id)
        )
        snapshot = FinalizationService(
            self.operations,
            self.kernel,
            self.graph,
            self.goals,
            ctf=self.ctf,
            execution=self.execution,
        ).snapshot(operation_id)
        goal = self.goals.current(operation_id)
        if goal is None:
            history = self.goals.list(operation_id)
            goal = history[-1] if history else None
        calls = [event for event in events if event.event_type == "model.call"]
        runs = self.execution.list_runs(operation_id)
        terminal = next(
            (
                str(event.payload.get("reason", ""))
                for event in reversed(events)
                if event.event_type == "chat.turn.end"
            ),
            "",
        )
        scope_refusals = sum(
            1
            for event in events
            if event.event_type == "tool.run.failed"
            and "outside the operation scope" in str(event.payload.get("reason", ""))
        )
        wall = (
            max(0.0, (events[-1].created_at - events[0].created_at).total_seconds())
            if events
            else 0.0
        )
        return OperationReadiness(
            operation_id=operation_id,
            hypotheses_total=coverage.total,
            hypotheses_closed=coverage.closed,
            open_ids=coverage.open_ids,
            testing_ids=coverage.testing_ids,
            goal_reached=self.goals.reached(operation_id),
            goal_state=goal.state.value if goal is not None else "",
            finalization_required=(
                goal is not None and goal.block_reason == "finalization_required"
            ),
            finalization_ready=snapshot.ready_to_complete,
            eligible_fact_ids=snapshot.eligible_fact_ids,
            pending_execution_ids=snapshot.pending_execution,
            pending_approval_ids=snapshot.pending_approvals,
            budget_steps_left=snapshot.budget_steps_left,
            budget_seconds_left=snapshot.budget_seconds_left,
            domain_findings=len(self.kernel.kernel.list_findings(operation_id)),
            terminal_reason=terminal,
            model_calls=len(calls),
            tool_runs=len(runs),
            failed_runs=sum(
                run.exit_code not in (0, None) or run.status is ToolRunStatus.UNKNOWN
                for run in runs
            ),
            artifacts=len(self.kernel.kernel.list_artifacts(operation_id)),
            approvals_requested=sum(
                event.event_type == "approval.requested" for event in events
            ),
            scope_refusals=scope_refusals,
            wall_seconds=round(wall, 3),
            cost_usd=round(
                sum(float(event.payload.get("estimated_cost_usd", 0.0)) for event in calls),
                6,
            ),
        )


__all__ = ["OperationReadiness", "ReadinessService"]
