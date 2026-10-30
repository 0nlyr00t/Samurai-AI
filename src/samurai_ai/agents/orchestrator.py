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

import threading
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from samurai_ai.agents.blackboard import Blackboard, make_note_event
from samurai_ai.agents.budgets import BudgetExceededError
from samurai_ai.agents.parser import (
    MAX_PARSE_RETRIES,
    corrective_prompt,
    parse_executor_output,
    parse_planner_output,
)
from samurai_ai.agents.specializations import profile_for
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
from samurai_ai.domain.kernel import (
    ArtifactError,
    FindingSeverity,
    KernelError,
    TaskStatus,
)
from samurai_ai.domain.models import CapabilityError, ModelCapability, ModelError
from samurai_ai.domain.operations import OperationEvent
from samurai_ai.domain.swarm import AgentSpecialization
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.skills import SkillService
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
        memory: CampaignMemoryService | None = None,
        skills: SkillService | None = None,
    ) -> None:
        self.models = models
        self.kernel = kernel
        self.execution = execution
        self.operations = operations
        self.runs = runs
        self.redactor = redactor or SecretRedactor()
        self.memory = memory
        self.skills = skills

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
        stop_event: threading.Event | None = None,
        read_only_only: bool = False,
        approval_resolver: Callable[[str], bool] | None = None,
        on_start: Callable[[AgentRun], None] | None = None,
        specialization: AgentSpecialization = AgentSpecialization.GENERAL,
        swarm_id: str | None = None,
        workspace: str | None = None,
        skill_names: Sequence[str] = (),
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
        if task.status is TaskStatus.PENDING:
            self.kernel.start_task(operation_id, task_id)
        elif task.status is not TaskStatus.IN_PROGRESS:
            raise AgentPlanError(
                f"task {task_id} cannot run from status {task.status.value}"
            )

        model, _ = self.models.resolve_model(candidates[0])
        if not (
            model.capabilities
            & {ModelCapability.STRUCTURED_OUTPUT, ModelCapability.TOOLS}
        ):
            raise CapabilityError(
                model.id, ("structured-output", ModelCapability.TOOLS.value)
            )

        extra_context, memory_entry_ids, skill_ids = self._resolve_context(
            workspace, skill_names
        )

        run = AgentRun(
            operation_id=operation_id,
            task_id=task_id,
            parent_run_id=parent_run_id,
            depth=depth,
            swarm_id=swarm_id,
            specialization=specialization,
            workspace=workspace,
            skills=tuple(skill_names),
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
                "specialization": specialization.value,
                "swarm_id": swarm_id,
                "workspace": workspace,
                "skills": list(skill_names),
                "memory_entry_ids": list(memory_entry_ids),
                "skill_ids": list(skill_ids),
            },
        )
        del operation
        model_approvals: dict[str, str] = {}
        if on_start is not None:
            on_start(run)
        try:
            steps = self._plan(
                operation_id,
                run,
                task.title,
                approval_id,
                extra_context,
                approval_resolver=approval_resolver,
                model_approvals=model_approvals,
                stop_event=stop_event,
            )
            self._execute_steps(
                operation_id,
                run,
                steps[:max_steps],
                approval_id,
                extra_context,
                stop_event=stop_event,
                read_only_only=read_only_only,
                approval_resolver=approval_resolver,
                model_approvals=model_approvals,
            )
            succeeded = run.status is AgentRunStatus.RUNNING
            if succeeded:
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
        except ModelError as exc:
            self._finish_failed(
                run, AgentRunStatus.FAILED, f"provider-error:{exc}"
            )
            return self.get_run(run.id)

        if succeeded:
            self.runs.finish(run)
            self._append(
                operation_id,
                "agent.run.finish",
                {"run_id": run.id, "status": run.status.value, "steps": len(steps)},
            )
        return self.get_run(run.id)

    def _resolve_context(
        self, workspace: str | None, skill_names: Sequence[str]
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """Resolve memory digest and skill fragments before the run is recorded.

        Fails fast (no run row, no events) when the workspace or a skill is
        unavailable.
        """
        sections: list[str] = []
        memory_entry_ids: tuple[str, ...] = ()
        skill_ids: tuple[str, ...] = ()
        if workspace is not None:
            if self.memory is None:
                raise AgentError("campaign memory is not configured")
            digest, memory_entry_ids = self.memory.digest(workspace)
            if digest:
                sections.append(
                    "Campaign memory (operator-curated, read-only):\n" + digest
                )
        if skill_names:
            if self.skills is None:
                raise AgentError("skills are not configured")
            fragment, skill_ids = self.skills.prompt_fragment(skill_names)
            if fragment:
                sections.append(
                    "Installed skills (trusted operator instructions):\n" + fragment
                )
        return "\n\n".join(sections), memory_entry_ids, skill_ids

    def _plan(
        self,
        operation_id: str,
        run: AgentRun,
        task_title: str,
        approval_id: str | None,
        extra_context: str,
        *,
        approval_resolver: Callable[[str], bool] | None = None,
        model_approvals: dict[str, str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> tuple[str, ...]:
        blackboard = Blackboard.from_events(self.operations.events(operation_id))
        context = self._blackboard_excerpt(blackboard, run.id)
        prompt = (
            f"{_PLANNER_INSTRUCTION}{profile_for(run.specialization).planner_focus}\n"
            f"Task: {task_title}\n"
            f"Blackboard:\n{context or '(empty)'}"
        )
        if extra_context:
            prompt += f"\n\n{extra_context}"
        output = self._call_model(
            operation_id,
            run,
            prompt,
            parse_planner_output,
            approval_id,
            approval_resolver=approval_resolver,
            model_approvals=model_approvals,
            stop_event=stop_event,
        )
        self._append(
            operation_id,
            "agent.step",
            {"run_id": run.id, "step": "plan", "steps": list(output.steps)},
        )
        return output.steps

    def _stop_requested(
        self, run: AgentRun, stop_event: threading.Event | None
    ) -> bool:
        """Cooperative cancellation: the operator's stop wins over the budget."""
        if stop_event is None or not stop_event.is_set():
            return False
        self._finish_failed(run, AgentRunStatus.FAILED, "cancelled by the operator")
        return True

    def _execute_steps(
        self,
        operation_id: str,
        run: AgentRun,
        steps: tuple[str, ...],
        approval_id: str | None,
        extra_context: str,
        *,
        stop_event: threading.Event | None = None,
        read_only_only: bool = False,
        approval_resolver: Callable[[str], bool] | None = None,
        model_approvals: dict[str, str] | None = None,
    ) -> None:
        consecutive_rejects = 0
        for index, step in enumerate(steps):
            if run.status is not AgentRunStatus.RUNNING:
                return
            if self._stop_requested(run, stop_event):
                return
            commands = self._execute_step_prompt(
                operation_id,
                run,
                step,
                index,
                approval_id,
                extra_context,
                approval_resolver=approval_resolver,
                model_approvals=model_approvals,
                stop_event=stop_event,
            )
            for command in commands:
                if self._stop_requested(run, stop_event):
                    return
                accepted = self._apply_command(
                    operation_id,
                    run,
                    command,
                    read_only_only=read_only_only,
                    approval_resolver=approval_resolver,
                )
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
        extra_context: str,
        *,
        approval_resolver: Callable[[str], bool] | None = None,
        model_approvals: dict[str, str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> tuple[AgentCommand, ...]:
        blackboard = Blackboard.from_events(self.operations.events(operation_id))
        prompt = (
            f"{_EXECUTOR_INSTRUCTION}{profile_for(run.specialization).executor_focus}\n"
            f"{self._tools_excerpt()}\n"
            f"Current step: {step}\n"
            f"Blackboard:\n{self._blackboard_excerpt(blackboard, run.id) or '(empty)'}"
        )
        if extra_context:
            prompt += f"\n\n{extra_context}"
        commands = self._call_model(
            operation_id,
            run,
            prompt,
            parse_executor_output,
            approval_id,
            approval_resolver=approval_resolver,
            model_approvals=model_approvals,
            stop_event=stop_event,
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
        *,
        approval_resolver: Callable[[str], bool] | None = None,
        model_approvals: dict[str, str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> _T:
        current = prompt
        for attempt in range(MAX_PARSE_RETRIES + 1):
            response = self._complete(
                operation_id,
                run,
                current,
                approval_id,
                approval_resolver=approval_resolver,
                model_approvals=model_approvals,
                stop_event=stop_event,
            )
            try:
                return parse(response.text)
            except AgentOutputError as exc:
                if attempt == MAX_PARSE_RETRIES:
                    raise
                current = corrective_prompt(prompt, exc)
        raise AgentOutputError("unreachable parser retry state")

    def _complete(
        self,
        operation_id: str,
        run: AgentRun,
        prompt: str,
        approval_id: str | None,
        *,
        approval_resolver: Callable[[str], bool] | None = None,
        model_approvals: dict[str, str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> Any:
        while True:
            if approval_id is None and model_approvals:
                approval_id = model_approvals.get("id")
            try:
                return self.models.complete(
                    operation_id,
                    run.model_route[0],
                    prompt,
                    fallbacks=tuple(run.model_route[1:]),
                    approval_id=approval_id,
                    extra_payload={"agent_run_id": run.id},
                )
            except ApprovalRequiredError as exc:
                if approval_resolver is None:
                    raise
                if not approval_resolver(exc.approval_id):
                    if stop_event is not None and stop_event.is_set():
                        raise AgentError("cancelled by the operator") from exc
                    raise
                self.kernel.approve(exc.approval_id, decided_by="subagent")
                approval_id = exc.approval_id
                if model_approvals is not None:
                    model_approvals["id"] = approval_id

    def _apply_command(
        self,
        operation_id: str,
        run: AgentRun,
        command: AgentCommand,
        *,
        read_only_only: bool = False,
        approval_resolver: Callable[[str], bool] | None = None,
    ) -> bool:
        try:
            if isinstance(command, RequestToolRunCommand):
                if read_only_only and not self._is_read_only(command.tool_id):
                    self._reject(
                        operation_id,
                        run,
                        command,
                        "plan mode allows read-only tool calls only",
                    )
                    return False
                self._command_tool_run(
                    operation_id, run, command, approval_resolver=approval_resolver
                )
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

    def _is_read_only(self, tool_id: str) -> bool:
        try:
            return self.execution.registry.get(tool_id).definition.read_only
        except ToolNotFoundError:
            return False

    def _command_tool_run(
        self,
        operation_id: str,
        run: AgentRun,
        command: RequestToolRunCommand,
        *,
        approval_resolver: Callable[[str], bool] | None = None,
    ) -> None:
        self.execution.registry.get(command.tool_id)
        result = self.execution.run_tool(
            operation_id,
            command.tool_id,
            command.params,
            task_id=run.task_id,
            approval_resolver=approval_resolver,
        )
        # Feed a bounded, already-redacted excerpt back into the blackboard so
        # the next executor step can actually see what the tool returned.
        excerpt = result.stdout.strip()[:1500]
        if excerpt:
            self._append_note(
                operation_id,
                run,
                NoteCommand(
                    type="note",
                    key=f"tool-result.{command.tool_id}",
                    value=excerpt,
                ),
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

    def _tools_excerpt(self) -> str:
        lines = [
            "Available tools (request_tool_run tool_id must be one of these):",
        ]
        for definition in self.execution.list_tools():
            description = definition.description.split(".")[0][:80]
            lines.append(
                f"- {definition.id} (risk {definition.risk_class.value}): {description}"
            )
        return "\n".join(lines)

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
