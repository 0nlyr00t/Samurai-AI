from __future__ import annotations

from typing import Any

from samurai_ai.domain.kernel import (
    ApprovalStatus,
    GateStatus,
    OperationPlan,
    PlanTask,
    StageGate,
    TaskStatus,
)
from samurai_ai.domain.operations import Operation, OperationEvent
from samurai_ai.domain.recovery import (
    OperationSnapshot,
    PlanDiff,
    RecoveryAction,
    RecoveryReport,
    ScopeDiff,
    diff_plans,
    diff_scopes,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.recovery import RecoveryRepository


class SnapshotNotFoundError(LookupError):
    pass


class RecoveryActionNotFoundError(LookupError):
    pass


class PlanNotFoundError(LookupError):
    pass


class RecoveryService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        recovery: RecoveryRepository,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.recovery = recovery

    def create_snapshot(self, operation_id: str, label: str = "") -> OperationSnapshot:
        operation = self.operations.get(operation_id)
        plan = self.kernel.kernel.get_current_plan(operation_id)
        data: dict[str, Any] = {
            "snapshot_version": 1,
            "operation": operation.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json") if plan is not None else None,
            "approvals": [
                approval.model_dump(mode="json")
                for approval in self.kernel.kernel.list_approvals(operation_id)
            ],
            "findings": [
                finding.model_dump(mode="json")
                for finding in self.kernel.kernel.list_findings(operation_id)
            ],
            "artifacts": [
                artifact.model_dump(mode="json")
                for artifact in self.kernel.kernel.list_artifacts(operation_id)
            ],
        }
        snapshot = OperationSnapshot(operation_id=operation_id, label=label, data=data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="snapshot.created",
            payload={"snapshot_id": snapshot.id, "label": label},
        )
        self.recovery.add_snapshot(snapshot, event)
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> OperationSnapshot:
        snapshot = self.recovery.get_snapshot(snapshot_id)
        if snapshot is None:
            raise SnapshotNotFoundError(f"snapshot not found: {snapshot_id}")
        return snapshot

    def list_snapshots(self, operation_id: str) -> list[OperationSnapshot]:
        self.operations.get(operation_id)
        return self.recovery.list_snapshots(operation_id)

    def restore_snapshot(self, snapshot_id: str, name: str | None = None) -> Operation:
        snapshot = self.get_snapshot(snapshot_id)
        source = Operation.model_validate(snapshot.data["operation"])
        operation = Operation(
            name=name or f"{source.name} (restored)",
            mode=source.mode,
            scope=source.scope,
            objective=source.objective,
        )
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.restored",
            payload={"snapshot_id": snapshot.id, "source_operation_id": source.id},
        )
        self.operations.repository.add(operation, event)
        plan_payload = snapshot.data.get("plan")
        if plan_payload is not None:
            source_plan = OperationPlan.model_validate(plan_payload)
            plan = _reset_plan(source_plan, operation.id)
            plan_event = OperationEvent(
                operation_id=operation.id,
                event_type="plan.restored",
                payload={"plan_id": plan.id, "source_plan_id": source_plan.id},
            )
            self.kernel.kernel.add_plan(plan, plan_event)
        return operation

    def fork_operation(self, operation_id: str, name: str | None = None) -> Operation:
        source = self.operations.get(operation_id)
        operation = Operation(
            name=name or f"{source.name} (fork)",
            mode=source.mode,
            scope=source.scope,
            objective=source.objective,
        )
        event = OperationEvent(
            operation_id=operation.id,
            event_type="operation.forked",
            payload={"source_operation_id": source.id},
        )
        self.operations.repository.add(operation, event)
        source_plan = self.kernel.kernel.get_current_plan(operation_id)
        if source_plan is not None:
            plan = _reset_plan(source_plan, operation.id)
            plan_event = OperationEvent(
                operation_id=operation.id,
                event_type="plan.forked",
                payload={"plan_id": plan.id, "source_plan_id": source_plan.id},
            )
            self.kernel.kernel.add_plan(plan, plan_event)
        return operation

    def begin_action(
        self,
        operation_id: str,
        action_type: str,
        description: str = "",
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RecoveryAction:
        self.operations.get(operation_id)
        action = RecoveryAction(
            operation_id=operation_id,
            task_id=task_id,
            action_type=action_type,
            description=description,
            payload=payload or {},
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="recovery.action.started",
            payload={"action_id": action.id, "action_type": action_type},
        )
        self.recovery.add_action(action, event)
        return action

    def complete_action(self, action_id: str) -> RecoveryAction:
        return self._finish_action(action_id, "completed")

    def fail_action(self, action_id: str, reason: str = "") -> RecoveryAction:
        return self._finish_action(action_id, "failed", reason)

    def reconcile_operation(self, operation_id: str, actor: str = "operator") -> RecoveryReport:
        self.operations.get(operation_id)
        marked = self.recovery.mark_in_flight_unknown(operation_id, actor)
        plan = self.kernel.kernel.get_current_plan(operation_id)
        resumable = (
            tuple(task.id for task in plan.tasks if task.status == TaskStatus.IN_PROGRESS)
            if plan is not None
            else ()
        )
        pending = tuple(
            approval.id
            for approval in self.kernel.kernel.list_approvals(
                operation_id, status=ApprovalStatus.PENDING
            )
        )
        return RecoveryReport(
            operation_id=operation_id,
            actions_marked_unknown=tuple(marked),
            resumable_tasks=resumable,
            pending_approvals=pending,
        )

    def plan_diff(self, operation_id: str, old_plan_id: str, new_plan_id: str) -> PlanDiff:
        self.operations.get(operation_id)
        old = self.kernel.kernel.get_plan(old_plan_id)
        if old is None or old.operation_id != operation_id:
            raise PlanNotFoundError(f"plan not found in operation {operation_id}: {old_plan_id}")
        new = self.kernel.kernel.get_plan(new_plan_id)
        if new is None or new.operation_id != operation_id:
            raise PlanNotFoundError(f"plan not found in operation {operation_id}: {new_plan_id}")
        return diff_plans(old, new)

    def scope_diff(self, old: ScopeContract, new: ScopeContract) -> ScopeDiff:
        return diff_scopes(old, new)

    def _finish_action(
        self, action_id: str, outcome: str, reason: str = ""
    ) -> RecoveryAction:
        action = self.recovery.get_action(action_id)
        if action is None:
            raise RecoveryActionNotFoundError(f"recovery action not found: {action_id}")
        expected_version = action.version
        if outcome == "completed":
            action.complete()
        else:
            action.fail()
        event = OperationEvent(
            operation_id=action.operation_id,
            event_type=f"recovery.action.{outcome}",
            payload={"action_id": action.id, "reason": reason},
        )
        self.recovery.save_action(action, expected_version, event)
        return action


def _reset_plan(source: OperationPlan, operation_id: str) -> OperationPlan:
    tasks = tuple(
        PlanTask(
            id=task.id,
            title=task.title,
            description=task.description,
            status=TaskStatus.PENDING,
            risk_class=task.risk_class,
            depends_on=task.depends_on,
            required_artifacts=task.required_artifacts,
        )
        for task in source.tasks
    )
    gates = tuple(
        StageGate(
            id=gate.id,
            name=gate.name,
            status=GateStatus.PENDING,
            required_artifacts=gate.required_artifacts,
        )
        for gate in source.gates
    )
    return OperationPlan(
        operation_id=operation_id,
        title=source.title,
        tasks=tasks,
        gates=gates,
        approved=False,
        is_current=True,
    )
