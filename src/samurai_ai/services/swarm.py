"""Parallel specialized agent swarms over independent plan tasks.

A swarm runs one governed agent run per selected task concurrently through a
bounded thread pool. Members are ordinary AgentRuns: same command whitelist,
same approvals/scope/budgets — nothing is bypassed. Specialization changes
only prompt focus. The swarm record and per-member linkage are persisted so a
swarm report can quantify wall time against the sequential estimate.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.kernel import Approval, OperationPlan, PlanTask, RiskClass, TaskStatus
from samurai_ai.domain.models import parse_model_id
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.swarm import (
    MAX_SWARM_WORKERS,
    AgentSpecialization,
    SwarmError,
    SwarmNotFoundError,
    SwarmRun,
    SwarmStatus,
    SwarmTaskError,
)
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.swarm import SwarmRunRepository


class SwarmService:
    def __init__(
        self,
        operations: OperationService,
        kernel: KernelService,
        orchestrator: AgentOrchestrator,
        swarms: SwarmRunRepository,
    ) -> None:
        self.operations = operations
        self.kernel = kernel
        self.orchestrator = orchestrator
        self.swarms = swarms

    def get_swarm(self, swarm_id: str) -> SwarmRun:
        swarm = self.swarms.get(swarm_id)
        if swarm is None:
            raise SwarmNotFoundError(f"swarm not found: {swarm_id}")
        return swarm

    def list_swarms(self, operation_id: str) -> list[SwarmRun]:
        self.operations.get(operation_id)
        return self.swarms.list_swarms(operation_id)

    def members(self, swarm_id: str) -> list[AgentRun]:
        self.get_swarm(swarm_id)
        return self.orchestrator.runs.list_swarm_members(swarm_id)

    def ready_tasks(self, operation_id: str) -> tuple[PlanTask, ...]:
        plan = self._require_plan(operation_id)
        return tuple(
            task
            for task in plan.tasks
            if task.status is TaskStatus.PENDING and self._dependencies_met(plan, task)
        )

    def prepare_model_approval(self, operation_id: str, route: Sequence[str]) -> Approval:
        if not route:
            raise SwarmError("swarm requires at least one model candidate")
        self.operations.get(operation_id)
        model, _ = self.orchestrator.models.resolve_model(route[0])
        remote = False
        for candidate in route:
            provider_id, _ = parse_model_id(candidate)
            if not self.orchestrator.models.registry.get_provider(provider_id).local:
                remote = True
                break
        risk = RiskClass.R2 if remote else RiskClass.R1
        return self.kernel.request_approval(
            operation_id,
            f"model.call {model.id}",
            risk,
            reason=f"swarm model route via {model.provider_id}",
            requested_by="operator",
        )

    def start(
        self,
        operation_id: str,
        task_ids: Sequence[str] | None,
        route: Sequence[str],
        *,
        workers: int = 2,
        specializations: dict[str, AgentSpecialization] | None = None,
        approval_id: str | None = None,
        created_by: str = "operator",
    ) -> SwarmRun:
        if not route:
            raise SwarmError("swarm requires at least one model candidate")
        if not 1 <= workers <= MAX_SWARM_WORKERS:
            raise SwarmError(
                f"workers must be between 1 and {MAX_SWARM_WORKERS}, got {workers}"
            )
        plan = self._require_plan(operation_id)
        selected = self._select_tasks(plan, task_ids, workers)
        specs = specializations or {}
        unknown_specs = sorted(set(specs) - {task.id for task in selected})
        if unknown_specs:
            raise SwarmTaskError(
                f"specializations reference tasks outside the swarm: "
                f"{', '.join(unknown_specs)}"
            )

        swarm = SwarmRun(
            operation_id=operation_id,
            workers=workers,
            task_ids=tuple(task.id for task in selected),
            model_route=tuple(route),
            created_by=created_by,
        )
        self.swarms.add(swarm)
        self._append(
            operation_id,
            "agent.swarm.start",
            {
                "swarm_id": swarm.id,
                "task_ids": list(swarm.task_ids),
                "workers": workers,
                "model_route": list(swarm.model_route),
                "specializations": {
                    task_id: spec.value for task_id, spec in sorted(specs.items())
                },
            },
        )
        try:
            for task in selected:
                self.kernel.start_task(operation_id, task.id)
        except Exception:
            self._finish(swarm, SwarmStatus.FAILED, reason="task start failed")
            raise

        results: dict[str, AgentRunStatus] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                task.id: pool.submit(
                    self._run_member,
                    operation_id,
                    swarm.id,
                    task.id,
                    list(route),
                    specs.get(task.id, AgentSpecialization.GENERAL),
                    approval_id,
                    created_by,
                )
                for task in selected
            }
            for task_id, future in futures.items():
                results[task_id] = future.result()

        completed = sum(1 for status in results.values() if status is AgentRunStatus.COMPLETED)
        if completed == len(results):
            final = SwarmStatus.COMPLETED
        elif completed == 0:
            final = SwarmStatus.FAILED
        else:
            final = SwarmStatus.PARTIAL
        self._finish(swarm, final)
        return self.get_swarm(swarm.id)

    def report(self, swarm_id: str) -> dict[str, Any]:
        swarm = self.get_swarm(swarm_id)
        members = self.orchestrator.runs.list_swarm_members(swarm_id)
        events = self.operations.events(swarm.operation_id)
        usage_by_run: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.event_type != "model.call":
                continue
            run_id = event.payload.get("agent_run_id")
            if not isinstance(run_id, str):
                continue
            usage = usage_by_run.setdefault(
                run_id, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            )
            usage["calls"] += 1
            usage["prompt_tokens"] += int(event.payload.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(event.payload.get("completion_tokens") or 0)
            usage["cost_usd"] += float(event.payload.get("estimated_cost_usd") or 0.0)

        member_rows: list[dict[str, Any]] = []
        sequential_estimate = 0.0
        for member in members:
            duration = None
            if member.finished_at is not None:
                duration = (member.finished_at - member.created_at).total_seconds()
                sequential_estimate += duration
            usage = usage_by_run.get(
                member.id, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            )
            member_rows.append(
                {
                    "run_id": member.id,
                    "task_id": member.task_id,
                    "status": member.status.value,
                    "specialization": member.specialization.value,
                    "duration_seconds": round(duration, 3) if duration is not None else None,
                    "usage": {**usage, "cost_usd": round(usage["cost_usd"], 6)},
                }
            )

        wall_time = None
        speedup = None
        if swarm.finished_at is not None:
            wall_time = (swarm.finished_at - swarm.created_at).total_seconds()
            if wall_time > 0 and sequential_estimate > 0:
                speedup = round(sequential_estimate / wall_time, 2)
        return {
            "id": swarm.id,
            "operation_id": swarm.operation_id,
            "status": swarm.status.value,
            "workers": swarm.workers,
            "task_ids": list(swarm.task_ids),
            "model_route": list(swarm.model_route),
            "members": member_rows,
            "wall_time_seconds": round(wall_time, 3) if wall_time is not None else None,
            "sequential_estimate_seconds": round(sequential_estimate, 3),
            "speedup": speedup,
            "usage": {
                "calls": sum(row["usage"]["calls"] for row in member_rows),
                "prompt_tokens": sum(row["usage"]["prompt_tokens"] for row in member_rows),
                "completion_tokens": sum(
                    row["usage"]["completion_tokens"] for row in member_rows
                ),
                "cost_usd": round(sum(row["usage"]["cost_usd"] for row in member_rows), 6),
            },
            "created_at": swarm.created_at.isoformat(),
            "finished_at": swarm.finished_at.isoformat() if swarm.finished_at else None,
        }

    def _run_member(
        self,
        operation_id: str,
        swarm_id: str,
        task_id: str,
        route: list[str],
        specialization: AgentSpecialization,
        approval_id: str | None,
        created_by: str,
    ) -> AgentRunStatus:
        try:
            run = self.orchestrator.run_task(
                operation_id,
                task_id,
                route,
                approval_id=approval_id,
                created_by=created_by,
                specialization=specialization,
                swarm_id=swarm_id,
            )
        except Exception as exc:
            self._append(
                operation_id,
                "agent.swarm.member-failed",
                {"swarm_id": swarm_id, "task_id": task_id, "error": str(exc)},
            )
            return AgentRunStatus.FAILED
        return run.status

    def _select_tasks(
        self,
        plan: OperationPlan,
        task_ids: Sequence[str] | None,
        workers: int,
    ) -> tuple[PlanTask, ...]:
        if task_ids is None:
            selected = tuple(
                task
                for task in plan.tasks
                if task.status is TaskStatus.PENDING
                and self._dependencies_met(plan, task)
            )
            if not selected:
                raise SwarmTaskError("no ready tasks in the current plan")
            return selected
        if len(set(task_ids)) != len(task_ids):
            raise SwarmTaskError("duplicate task ids in swarm selection")
        by_id = {task.id: task for task in plan.tasks}
        selected_list: list[PlanTask] = []
        for task_id in task_ids:
            task = by_id.get(task_id)
            if task is None:
                raise SwarmTaskError(f"unknown task {task_id} in the current plan")
            if task.status is not TaskStatus.PENDING:
                raise SwarmTaskError(
                    f"task {task_id} is not pending (status {task.status.value})"
                )
            if not self._dependencies_met(plan, task):
                raise SwarmTaskError(f"task {task_id} has unfinished dependencies")
            selected_list.append(task)
        if len(selected_list) > workers * 4:
            raise SwarmTaskError(
                f"swarm selection of {len(selected_list)} tasks exceeds the "
                f"limit of {workers * 4} for {workers} workers"
            )
        return tuple(selected_list)

    @staticmethod
    def _dependencies_met(plan: OperationPlan, task: PlanTask) -> bool:
        by_id = {item.id: item for item in plan.tasks}
        for dependency_id in task.depends_on:
            dependency = by_id.get(dependency_id)
            if dependency is None or dependency.status not in (
                TaskStatus.DONE,
                TaskStatus.SKIPPED,
            ):
                return False
        return True

    def _require_plan(self, operation_id: str) -> OperationPlan:
        self.operations.get(operation_id)
        plan = self.kernel.get_current_plan(operation_id)
        if plan is None:
            raise SwarmError(f"operation {operation_id} has no current plan")
        return plan

    def _finish(self, swarm: SwarmRun, status: SwarmStatus, reason: str | None = None) -> None:
        if swarm.status is SwarmStatus.RUNNING:
            swarm.finish(status)
            self.swarms.finish(swarm)
        payload: dict[str, Any] = {"swarm_id": swarm.id, "status": status.value}
        if reason:
            payload["reason"] = reason
        self._append(swarm.operation_id, "agent.swarm.finish", payload)

    def _append(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.operations.repository.append_event(
            OperationEvent(operation_id=operation_id, event_type=event_type, payload=payload)
        )
