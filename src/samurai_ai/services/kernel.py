from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from samurai_ai.domain.kernel import (
    Approval,
    ApprovalStateError,
    ApprovalStatus,
    Artifact,
    ArtifactError,
    ArtifactKind,
    Finding,
    FindingSeverity,
    FindingStatus,
    GateStateError,
    GateStatus,
    KernelError,
    OperationPlan,
    PlanTask,
    RiskClass,
    StageGate,
    TaskStateError,
    TaskStatus,
)
from samurai_ai.domain.operations import Operation, OperationEvent, OperationStatus
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.kernel import KernelConflictError, KernelRepository

_PLAN_CONFLICT_RETRIES = 3


class KernelPreconditionError(KernelError):
    pass


class PolicyViolationError(KernelError):
    pass


class ApprovalNotFoundError(LookupError):
    pass


class FindingNotFoundError(LookupError):
    pass


class KernelSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    tasks_done: int
    tasks_total: int
    gates_passed: int
    gates_total: int
    pending_approvals: int
    artifacts: int
    findings: int


class KernelService:
    def __init__(self, operations: OperationService, kernel: KernelRepository) -> None:
        self.operations = operations
        self.kernel = kernel

    def create_plan(
        self,
        operation_id: str,
        title: str,
        tasks: Iterable[PlanTask],
        gates: Iterable[StageGate],
    ) -> OperationPlan:
        self.operations.get(operation_id)
        if self.kernel.get_current_plan(operation_id) is not None:
            raise KernelPreconditionError(
                f"operation {operation_id} already has a current plan; use revise_plan"
            )
        plan = OperationPlan(
            operation_id=operation_id,
            title=title,
            tasks=tuple(tasks),
            gates=tuple(gates),
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="plan.created",
            payload={"plan_id": plan.id, "title": plan.title},
        )
        self.kernel.add_plan(plan, event)
        return plan

    def revise_plan(
        self,
        operation_id: str,
        title: str,
        tasks: Iterable[PlanTask],
        gates: Iterable[StageGate],
    ) -> OperationPlan:
        self.operations.get(operation_id)
        current = self._get_plan(operation_id)
        plan = OperationPlan(
            operation_id=operation_id,
            title=title,
            tasks=tuple(tasks),
            gates=tuple(gates),
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="plan.revised",
            payload={"plan_id": plan.id, "supersedes": current.id},
        )
        self.kernel.replace_current_plan(plan, event)
        return plan

    def approve_plan(self, operation_id: str) -> OperationPlan:
        plan = self._get_plan(operation_id)
        if plan.approved:
            return plan
        expected_version = plan.version
        plan.approved = True
        plan.touch()
        event = OperationEvent(
            operation_id=operation_id,
            event_type="plan.approved",
            payload={"plan_id": plan.id},
        )
        self.kernel.save_plan(plan, expected_version, event)
        return plan

    def get_current_plan(self, operation_id: str) -> OperationPlan | None:
        self.operations.get(operation_id)
        return self.kernel.get_current_plan(operation_id)

    def start_task(self, operation_id: str, task_id: str) -> OperationPlan:
        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            task = plan.task_by_id(task_id)
            if task is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
            if task.status != TaskStatus.PENDING:
                raise TaskStateError(
                    f"task {task_id} cannot start from {task.status.value}"
                )
            for dependency_id in task.depends_on:
                dependency = plan.task_by_id(dependency_id)
                if dependency is None or dependency.status not in (
                    TaskStatus.DONE,
                    TaskStatus.SKIPPED,
                ):
                    raise TaskStateError(
                        f"task {task_id} cannot start before dependency {dependency_id} finishes"
                    )
            task.status = TaskStatus.IN_PROGRESS
            return "task.started", {"plan_id": plan.id, "task_id": task_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def complete_task(self, operation_id: str, task_id: str) -> OperationPlan:
        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            task = plan.task_by_id(task_id)
            if task is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
            if task.status != TaskStatus.IN_PROGRESS:
                raise TaskStateError(
                    f"task {task_id} cannot complete from {task.status.value}"
                )
            for requirement in task.required_artifacts:
                if not self.kernel.artifact_exists(operation_id, requirement, task_id=task_id):
                    raise ArtifactError(
                        f"task {task_id} is missing required artifact {requirement!r}"
                    )
            task.status = TaskStatus.DONE
            return "task.completed", {"plan_id": plan.id, "task_id": task_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def reset_task(self, operation_id: str, task_id: str) -> OperationPlan:
        """Return a task to pending: after a stopped child, or to redo it."""

        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            task = plan.task_by_id(task_id)
            if task is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
            if task.status is TaskStatus.PENDING:
                return "", {}
            task.status = TaskStatus.PENDING
            return "task.reset", {"plan_id": plan.id, "task_id": task_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def skip_task(self, operation_id: str, task_id: str) -> OperationPlan:
        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            task = plan.task_by_id(task_id)
            if task is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
            if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED):
                raise TaskStateError(
                    f"task {task_id} cannot skip from {task.status.value}"
                )
            task.status = TaskStatus.SKIPPED
            return "task.skipped", {"plan_id": plan.id, "task_id": task_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def block_task(self, operation_id: str, task_id: str) -> OperationPlan:
        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            task = plan.task_by_id(task_id)
            if task is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
            if task.status in (TaskStatus.DONE, TaskStatus.BLOCKED):
                raise TaskStateError(
                    f"task {task_id} cannot block from {task.status.value}"
                )
            task.status = TaskStatus.BLOCKED
            return "task.blocked", {"plan_id": plan.id, "task_id": task_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def pass_gate(self, operation_id: str, gate_id: str) -> OperationPlan:
        def mutate(plan: OperationPlan) -> tuple[str, dict[str, Any]]:
            gate = plan.gate_by_id(gate_id)
            if gate is None:
                raise GateStateError(f"unknown gate {gate_id} in plan {plan.id}")
            if gate.status == GateStatus.PASSED:
                return "", {}
            for requirement in gate.required_artifacts:
                if not self.kernel.artifact_exists(operation_id, requirement, gate_id=gate_id):
                    raise ArtifactError(
                        f"gate {gate_id} is missing required artifact {requirement!r}"
                    )
            gate.status = GateStatus.PASSED
            return "gate.passed", {"plan_id": plan.id, "gate_id": gate_id}

        return self._apply_plan_mutation(operation_id, mutate)

    def _apply_plan_mutation(
        self,
        operation_id: str,
        mutate: Callable[[OperationPlan], tuple[str, dict[str, Any]]],
    ) -> OperationPlan:
        for attempt in range(_PLAN_CONFLICT_RETRIES):
            plan = self._get_plan(operation_id)
            expected_version = plan.version
            event_type, payload = mutate(plan)
            if not event_type:
                return plan
            plan.touch()
            event = OperationEvent(
                operation_id=operation_id, event_type=event_type, payload=payload
            )
            try:
                self.kernel.save_plan(plan, expected_version, event)
            except KernelConflictError:
                if attempt == _PLAN_CONFLICT_RETRIES - 1:
                    raise
                continue
            return plan
        raise KernelConflictError(f"plan for operation {operation_id} is contended")

    def request_approval(
        self,
        operation_id: str,
        action: str,
        risk_class: RiskClass,
        task_id: str | None = None,
        reason: str = "",
        requested_by: str = "system",
    ) -> Approval:
        self.operations.get(operation_id)
        approval = Approval(
            operation_id=operation_id,
            task_id=task_id,
            action=action,
            reason=reason,
            risk_class=risk_class,
            requested_by=requested_by,
        )
        if risk_class in (RiskClass.R0, RiskClass.R1):
            approval.auto = True
            approval.status = ApprovalStatus.APPROVED
            approval.decided_by = requested_by
            approval.decided_at = datetime.now(UTC)
            event = OperationEvent(
                operation_id=operation_id,
                event_type="approval.auto-approved",
                payload={"approval_id": approval.id, "action": action},
            )
            self.kernel.add_approval(approval, event)
            return approval
        if risk_class is RiskClass.R4:
            approval.auto = True
            approval.status = ApprovalStatus.REJECTED
            approval.decided_by = "policy"
            approval.decided_at = datetime.now(UTC)
            event = OperationEvent(
                operation_id=operation_id,
                event_type="approval.auto-rejected",
                payload={"approval_id": approval.id, "action": action},
            )
            self.kernel.add_approval(approval, event)
            raise PolicyViolationError(
                f"action {action!r} is denied by policy for risk class {risk_class.value}"
            )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="approval.requested",
            payload={"approval_id": approval.id, "action": action},
        )
        self.kernel.add_approval(approval, event)
        return approval

    def approve(self, approval_id: str, decided_by: str) -> Approval:
        return self._decide(approval_id, decided_by, ApprovalStatus.APPROVED)

    def reject(self, approval_id: str, decided_by: str) -> Approval:
        return self._decide(approval_id, decided_by, ApprovalStatus.REJECTED)

    def register_artifact(
        self,
        operation_id: str,
        kind: ArtifactKind,
        content: str,
        summary: str = "",
        requirement: str | None = None,
        task_id: str | None = None,
        gate_id: str | None = None,
        finding_id: str | None = None,
        created_by: str = "system",
    ) -> Artifact:
        self.operations.get(operation_id)
        if task_id is not None:
            plan = self._get_plan(operation_id)
            if plan.task_by_id(task_id) is None:
                raise TaskStateError(f"unknown task {task_id} in plan {plan.id}")
        if gate_id is not None:
            plan = self._get_plan(operation_id)
            if plan.gate_by_id(gate_id) is None:
                raise GateStateError(f"unknown gate {gate_id} in plan {plan.id}")
        if finding_id is not None:
            finding = self._get_finding(finding_id)
            if finding.operation_id != operation_id:
                raise FindingNotFoundError(
                    f"finding {finding_id} does not belong to operation {operation_id}"
                )

        data = content.encode("utf-8")
        artifact_id = f"art_{uuid4().hex[:12]}"
        relative_path = f"artifacts/{operation_id}/{artifact_id}.txt"
        artifact = Artifact(
            id=artifact_id,
            operation_id=operation_id,
            kind=kind,
            summary=summary,
            requirement=requirement,
            task_id=task_id,
            gate_id=gate_id,
            finding_id=finding_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            path=relative_path,
            created_by=created_by,
        )
        directory = self.kernel.artifact_root / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{artifact.id}.txt").write_bytes(data)
        event = OperationEvent(
            operation_id=operation_id,
            event_type="artifact.registered",
            payload={
                "artifact_id": artifact.id,
                "kind": kind.value,
                "requirement": requirement,
                "task_id": task_id,
                "gate_id": gate_id,
                "finding_id": finding_id,
            },
        )
        self.kernel.add_artifact(artifact, event)
        return artifact

    def create_finding(
        self,
        operation_id: str,
        title: str,
        severity: FindingSeverity = FindingSeverity.INFO,
        summary: str = "",
        remediation: str = "",
    ) -> Finding:
        self.operations.get(operation_id)
        finding = Finding(
            operation_id=operation_id,
            title=title,
            severity=severity,
            summary=summary,
            remediation=remediation,
        )
        event = OperationEvent(
            operation_id=operation_id,
            event_type="finding.created",
            payload={"finding_id": finding.id, "severity": severity.value},
        )
        self.kernel.add_finding(finding, event)
        return finding

    def attach_evidence(self, finding_id: str, artifact_id: str) -> Finding:
        finding = self._get_finding(finding_id)
        artifact = self.kernel.get_artifact(artifact_id)
        if artifact is None or artifact.operation_id != finding.operation_id:
            raise ArtifactError(
                f"evidence artifact {artifact_id} not found in operation {finding.operation_id}"
            )
        expected_version = finding.version
        finding.attach_evidence(artifact_id)
        event = OperationEvent(
            operation_id=finding.operation_id,
            event_type="finding.evidence-attached",
            payload={"finding_id": finding_id, "artifact_id": artifact_id},
        )
        self.kernel.save_finding(finding, expected_version, event)
        return finding

    def validate_finding(self, finding_id: str) -> Finding:
        finding = self._get_finding(finding_id)
        expected_version = finding.version
        if finding.status == FindingStatus.CANDIDATE:
            finding.begin_validation()
        existing = [
            evidence_id
            for evidence_id in finding.evidence_ids
            if (artifact := self.kernel.get_artifact(evidence_id)) is not None
            and artifact.operation_id == finding.operation_id
        ]
        if not existing:
            raise ArtifactError(f"finding {finding_id} has no existing evidence artifacts")
        finding.mark_validated()
        event = OperationEvent(
            operation_id=finding.operation_id,
            event_type="finding.validated",
            payload={"finding_id": finding_id},
        )
        self.kernel.save_finding(finding, expected_version, event)
        return finding

    def mark_false_positive(self, finding_id: str) -> Finding:
        finding = self._get_finding(finding_id)
        expected_version = finding.version
        finding.mark_false_positive()
        event = OperationEvent(
            operation_id=finding.operation_id,
            event_type="finding.marked-false-positive",
            payload={"finding_id": finding_id},
        )
        self.kernel.save_finding(finding, expected_version, event)
        return finding

    def mark_duplicate(self, finding_id: str, original_id: str) -> Finding:
        finding = self._get_finding(finding_id)
        original = self.kernel.get_finding(original_id)
        if original is None or original.operation_id != finding.operation_id:
            raise FindingNotFoundError(
                f"original finding {original_id} not found in operation {finding.operation_id}"
            )
        expected_version = finding.version
        finding.mark_duplicate(original_id)
        event = OperationEvent(
            operation_id=finding.operation_id,
            event_type="finding.marked-duplicate",
            payload={"finding_id": finding_id, "duplicate_of": original_id},
        )
        self.kernel.save_finding(finding, expected_version, event)
        return finding

    def mark_reported(self, finding_id: str) -> Finding:
        finding = self._get_finding(finding_id)
        expected_version = finding.version
        finding.mark_reported()
        event = OperationEvent(
            operation_id=finding.operation_id,
            event_type="finding.reported",
            payload={"finding_id": finding_id},
        )
        self.kernel.save_finding(finding, expected_version, event)
        return finding

    def transition_operation(self, operation_id: str, target: OperationStatus) -> Operation:
        operation = self.operations.get(operation_id)
        stage = (operation.status, target)
        if stage == (OperationStatus.PLANNING, OperationStatus.APPROVAL):
            self._get_plan(operation_id)
        elif stage == (OperationStatus.APPROVAL, OperationStatus.EXECUTION):
            plan = self._get_plan(operation_id)
            if not plan.approved:
                raise KernelPreconditionError(f"current plan {plan.id} is not approved")
            pending = self.kernel.list_approvals(operation_id, status=ApprovalStatus.PENDING)
            if pending:
                raise KernelPreconditionError(
                    f"operation {operation_id} has {len(pending)} pending approvals"
                )
        elif stage == (OperationStatus.VERIFICATION, OperationStatus.REPORTING):
            plan = self._get_plan(operation_id)
            open_gates = [gate.id for gate in plan.gates if gate.status != GateStatus.PASSED]
            if open_gates:
                raise KernelPreconditionError(f"gates not passed: {', '.join(open_gates)}")
        elif stage == (OperationStatus.REPORTING, OperationStatus.COMPLETE):
            current_plan = self.kernel.get_current_plan(operation_id)
            if current_plan is not None:
                unfinished = [
                    task.id
                    for task in current_plan.tasks
                    if task.status not in (TaskStatus.DONE, TaskStatus.SKIPPED)
                ]
                if unfinished:
                    raise KernelPreconditionError(
                        f"tasks not finished: {', '.join(unfinished)}"
                    )
            open_findings = [
                finding.id
                for finding in self.kernel.list_findings(operation_id)
                if finding.status in (FindingStatus.CANDIDATE, FindingStatus.VALIDATING)
            ]
            if open_findings:
                raise KernelPreconditionError(
                    f"findings not resolved: {', '.join(open_findings)}"
                )
        return self.operations.transition(operation_id, target)

    def summary(self, operation_id: str) -> KernelSummary:
        self.operations.get(operation_id)
        plan = self.kernel.get_current_plan(operation_id)
        tasks = plan.tasks if plan is not None else ()
        gates = plan.gates if plan is not None else ()
        return KernelSummary(
            operation_id=operation_id,
            tasks_done=sum(
                1 for task in tasks if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
            ),
            tasks_total=len(tasks),
            gates_passed=sum(1 for gate in gates if gate.status == GateStatus.PASSED),
            gates_total=len(gates),
            pending_approvals=len(
                self.kernel.list_approvals(operation_id, status=ApprovalStatus.PENDING)
            ),
            artifacts=len(self.kernel.list_artifacts(operation_id)),
            findings=len(self.kernel.list_findings(operation_id)),
        )

    def _get_plan(self, operation_id: str) -> OperationPlan:
        plan = self.kernel.get_current_plan(operation_id)
        if plan is None:
            raise KernelPreconditionError(f"operation {operation_id} has no current plan")
        return plan

    def _get_approval(self, approval_id: str) -> Approval:
        approval = self.kernel.get_approval(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(f"approval not found: {approval_id}")
        return approval

    def _get_finding(self, finding_id: str) -> Finding:
        finding = self.kernel.get_finding(finding_id)
        if finding is None:
            raise FindingNotFoundError(f"finding not found: {finding_id}")
        return finding

    def _decide(
        self, approval_id: str, decided_by: str, decision: ApprovalStatus
    ) -> Approval:
        approval = self._get_approval(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise ApprovalStateError(
                f"approval {approval_id} was already {approval.status.value}"
            )
        expected_version = approval.version
        approval.status = decision
        approval.decided_by = decided_by
        approval.decided_at = datetime.now(UTC)
        approval.version += 1
        event = OperationEvent(
            operation_id=approval.operation_id,
            event_type="approval.decided",
            payload={
                "approval_id": approval_id,
                "decision": decision.value,
                "decided_by": decided_by,
            },
        )
        self.kernel.save_approval(approval, expected_version, event)
        return approval
