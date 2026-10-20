"""Planner/Executor/Parser/Verifier orchestration for governed agent runs.

The model never touches state directly: it may only emit structured command
JSON from a closed whitelist, and every command is validated and executed
through the governed services (ToolExecutionService with approvals/scope/
recovery, KernelService with policy preconditions). Rejected commands are
logged as agent.command.rejected events without side effects. Budgets are
checked by the governed ModelService before every model call; a
BudgetExceededError ends the run as budget-exceeded with state preserved in
the journal (resume by starting a new run — mid-run resume is intentionally
not implemented).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from samurai_ai.agents.blackboard import Blackboard, make_note_event
from samurai_ai.agents.budgets import BudgetExceededError
from samurai_ai.agents.parser import (
    MAX_PARSE_RETRIES,
    corrective_prompt,
    parse_executor_output,
    parse_planner_output,
)
from samurai_ai.domain.agents import (
    AgentCommand,
    AgentDepthError,
    AgentError,
    AgentOutputError,
    AgentPlanError,
    AgentRun,
    AgentRunNotFoundError,
    AgentRunStatus,
    CompleteTaskCommand,
    FailCommand,
    NoteCommand,
    ProposeFindingCommand,
    RequestToolRunCommand,
    command_summary,
)
from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    SecretRedactor,
    ToolExecutionError,
    ToolNotFoundError,
)
from samurai_ai.domain.kernel import ArtifactError, FindingSeverity, KernelError
from samurai_ai.domain.models import CapabilityError, ModelCapability
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.agents import AgentRunRepository

_T = TypeVar("_T")

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_DEPTH = 1
MAX_CONSECUTIVE_REJECTS = 3
_MAX_BLACKBOARD_ITEMS = 20

_PLANNER_INSTRUCTION = (
    "You are the planner of a governed security operation. Decompose the "
    "task into at most 4 concrete steps. Respond with exactly one JSON "
    'object: {"steps": ["step one", "step two"]}. No prose.'
)
_EXECUTOR_INSTRUCTION = (
    "You are the executor of a governed security operation. Emit exactly one "
    'JSON object: {"commands": [...]}. Allowed command types: '
    'request_tool_run {"type":"request_tool_run","tool_id":...,"params":{...}}, '
    'note {"type":"note","key":...,"value":...}, propose_finding '
    '{"type":"propose_finding","title":...,"summary":...,"severity":...,'
    '"artifact_ids":[...]}, complete_task {"type":"complete_task"}, fail '
    '{"type":"fail","reason":...}. No prose.'
)


class AgentOrchestrator:
    def __init__(
        self,
        models: ModelService,
        kernel: KernelService,
        execution: ToolExecutionService,
        operations: OperationService,
        runs: AgentRunRepository,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.models = models
        self.kernel = kernel
        self.execution = execution
        self.operations = operations
        self.runs = runs
        self.redactor = redactor or SecretRedactor()

    def get_run(self, run_id: str) -> AgentRun:
        run = self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError(f"agent run not found: {run_id}")
        return run

    def list_runs(self, operation_id: str) -> list[AgentRun]:
        self.operations.get(operation_id)
        return self.runs.list_runs(operation_id)

    def run_task(
        self,
        operation_id: str,
        task_id: str,
        candidates: list[str],
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        parent_run_id: str | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
        approval_id: str | None = None,
        created_by: str = "operator",
    ) -> AgentRun:
        if not candidates:
            raise AgentError("agent run requires at least one model candidate")
        operation = self.operations.get(operation_id)
        plan = self.kernel.get_current_plan(operation_id)
        if plan is None:
            raise AgentPlanError(f"operation {operation_id} has no current plan")
        task = plan.task_by_id(task_id)
        if task is None:
            raise AgentPlanError(f"unknown task {task_id} in plan {plan.id}")

        depth = 0
        if parent_run_id is not None:
            parent = self.get_run(parent_run_id)
            depth = parent.depth + 1
            if depth > max_depth:
                raise AgentDepthError(
                    f"subagent depth {depth} exceeds max_depth {max_depth} "
                    f"(parent run {parent_run_id})"
                )

        model, _ = self.models.resolve_model(candidates[0])
        if not (
            model.capabilities
            & {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOLS}
        ):
            raise CapabilityError(
                model.id, ("structured-output", ModelCapability.TOOLS.value)
            )

        run = AgentRun(
            operation_id=operation_id,
            task_id=task_id,
            parent_run_id=parent_run_id,
            depth=depth,
            model_route=tuple(candidates),
            created_by=created_by,
        )
        self.runs.add(run)
        self._append(
            operation_id,
            "agent.run.start",
            {
                "run_id": run.id,
                "task_id": task_id,
                "candidates": list(candidates),
                "depth": depth,
            },
        )
        del operation
        try:
            steps = self._plan(operation_id, run, task.title, approval_id)
            self._execute_steps(operation_id, run, steps[:max_steps], approval_id)
            if run.status is AgentRunStatus.RUNNING:
                run.finish(AgentRunStatus.COMPLETED)
        except BudgetExceededError as exc:
            self._finish_failed(run, AgentRunStatus.BUDGET_EXCEEDED, str(exc))
            return self.get_run(run.id)
        except ApprovalRequiredError as exc:
            self._finish_failed(
                run, AgentRunStatus.FAILED, f"approval-required:{exc.approval_id}"
            )
            raise
        except (AgentOutputError, AgentError) as exc:
            self._finish_failed(run, AgentRunStatus.FAILED, str(exc))
            return self.get_run(run.id)

        self.runs.finish(run)
        self._append(
            operation_id,
            "agent.run.finish",
            {"run_id": run.id, "status": run.status.value, "steps": len(steps)},
        )
        return run

    def _plan(
        self,
        operation_id: str,
        run: AgentRun,
        task_title: str,
        approval_id: str | None,
    ) -> tuple[str, ...]:
        blackboard = Blackboard.from_events(self.operations.events(operation_id))
        context = self._blackboard_excerpt(blackboard, run.id)
        prompt = (
            f"{_PLANNER_INSTRUCTION}\nTask: {task_title}\n"
            f"Blackboard:\n{context or '(empty)'}"
        )
        output = self._call_model(
            operation_id, run, prompt, parse_planner_output, approval_id
        )
        self._append(
            operation_id,
            "agent.step",
            {"run_id": run.id, "step": "plan", "steps": list(output.steps)},
        )
        return output.steps

    def _execute_steps(
        self,
        operation_id: str,
        run: AgentRun,
        steps: tuple[str, ...],
        approval_id: str | None,
    ) -> None:
        consecutive_rejects = 0
        for index, step in enumerate(steps):
            if run.status is not AgentRunStatus.RUNNING:
                return
            commands = self._execute_step_prompt(operation_id, run, step, index, approval_id)
            for command in commands:
                accepted = self._apply_command(operation_id, run, command)
                if accepted:
                    consecutive_rejects = 0
                else:
                    consecutive_rejects += 1
                    if consecutive_rejects > MAX_CONSECUTIVE_REJECTS:
                        raise AgentError(
                            f"too many consecutive rejected commands "
                            f"(>{MAX_CONSECUTIVE_REJECTS})"
                        )
                if run.status is not AgentRunStatus.RUNNING:
                    return

    def _execute_step_prompt(
        self,
        operation_id: str,
        run: AgentRun,
        step: str,
        index: int,
        approval_id: str | None,
    ) -> tuple[AgentCommand, ...]:
        blackboard = Blackboard.from_events(self.operations.events(operation_id))
        prompt = (
            f"{_EXECUTOR_INSTRUCTION}\nCurrent step: {step}\n"
            f"Blackboard:\n{self._blackboard_excerpt(blackboard, run.id) or '(empty)'}"
        )
        commands = self._call_model(
            operation_id, run, prompt, parse_executor_output, approval_id
        )
        self._append(
            operation_id,
            "agent.step",
            {
                "run_id": run.id,
                "step": index,
                "title": step,
                "commands": [command_summary(command) for command in commands],
            },
        )
        return commands

    def _call_model(
        self,
        operation_id: str,
        run: AgentRun,
        prompt: str,
        parse: Callable[[str], _T],
        approval_id: str | None,
    ) -> _T:
        current = prompt
        for attempt in range(MAX_PARSE_RETRIES + 1):
            response = self.models.complete(
                operation_id,
                run.model_route[0],
                current,
                fallbacks=tuple(run.model_route[1:]),
                approval_id=approval_id,
                extra_payload={"agent_run_id": run.id},
            )
            try:
                return parse(response.text)
            except AgentOutputError as exc:
                if attempt == MAX_PARSE_RETRIES:
                    raise
                current = corrective_prompt(prompt, exc)
        raise AgentOutputError("unreachable parser retry state")

    def _apply_command(
        self, operation_id: str, run: AgentRun, command: AgentCommand
    ) -> bool:
        try:
            if isinstance(command, RequestToolRunCommand):
                self._command_tool_run(operation_id, run, command)
            elif isinstance(command, NoteCommand):
                try:
                    self._append_note(operation_id, run, command)
                except ValueError as exc:
                    self._reject(operation_id, run, command, f"invalid note key: {exc}")
                    return False
            elif isinstance(command, ProposeFindingCommand):
                self._command_propose_finding(operation_id, run, command)
            elif isinstance(command, CompleteTaskCommand):
                self.kernel.complete_task(operation_id, run.task_id)
            elif isinstance(command, FailCommand):
                raise AgentError(f"agent requested failure: {command.reason}")
            else:
                raise AgentError(f"unsupported command type: {command!r}")
        except ApprovalRequiredError as exc:
            self._reject(operation_id, run, command, f"approval-required:{exc.approval_id}")
            return False
        except (ToolNotFoundError, ToolExecutionError) as exc:
            self._reject(operation_id, run, command, str(exc))
            return False
        except (KernelError, ArtifactError) as exc:
            self._reject(operation_id, run, command, str(exc))
            return False
        return True

    def _command_tool_run(
        self, operation_id: str, run: AgentRun, command: RequestToolRunCommand
    ) -> None:
        self.execution.registry.get(command.tool_id)
        self.execution.run_tool(
            operation_id,
            command.tool_id,
            command.params,
            task_id=run.task_id,
        )

    def _command_propose_finding(
        self, operation_id: str, run: AgentRun, command: ProposeFindingCommand
    ) -> None:
        for artifact_id in command.artifact_ids:
            artifact = self.kernel.kernel.get_artifact(artifact_id)
            if artifact is None or artifact.operation_id != operation_id:
                raise ArtifactError(
                    f"propose_finding references unknown artifact {artifact_id}"
                )
        finding = self.kernel.create_finding(
            operation_id,
            command.title,
            FindingSeverity(command.severity),
            summary=command.summary,
        )
        for artifact_id in command.artifact_ids:
            self.kernel.attach_evidence(finding.id, artifact_id)
        self._append(
            operation_id,
            "agent.step",
            {
                "run_id": run.id,
                "step": "finding-proposed",
                "finding_id": finding.id,
                "title": command.title,
            },
        )

    def _append_note(
        self, operation_id: str, run: AgentRun, command: NoteCommand
    ) -> None:
        key = command.key
        if not key.startswith("shared.") and not key.startswith(f"run.{run.id}."):
            key = f"run.{run.id}.{key}"
        self._append_event(make_note_event(operation_id, run.id, key, command.value, self.redactor))

    def _reject(
        self, operation_id: str, run: AgentRun, command: AgentCommand, reason: str
    ) -> None:
        self._append(
            operation_id,
            "agent.command.rejected",
            {
                "run_id": run.id,
                "command": command_summary(command),
                "reason": reason,
            },
        )

    def _finish_failed(self, run: AgentRun, status: AgentRunStatus, reason: str) -> None:
        if run.status is AgentRunStatus.RUNNING:
            run.finish(status)
            self.runs.finish(run)
        self._append(
            run.operation_id,
            "agent.run.finish",
            {"run_id": run.id, "status": status.value, "reason": reason},
        )

    def _blackboard_excerpt(self, blackboard: Blackboard, run_id: str) -> str:
        visible = blackboard.visible_for(run_id)
        lines = [f"{key}={value}" for key, value in list(visible.items())[:_MAX_BLACKBOARD_ITEMS]]
        return "\n".join(lines)

    def _append(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._append_event(
            OperationEvent(
                operation_id=operation_id, event_type=event_type, payload=payload
            )
        )

    def _append_event(self, event: OperationEvent) -> None:
        self.operations.repository.append_event(event)
