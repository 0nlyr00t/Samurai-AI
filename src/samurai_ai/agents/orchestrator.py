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

import json
import threading
import time
from collections import deque
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
    AskOperatorCommand,
    CompleteTaskCommand,
    FailCommand,
    NoteCommand,
    ProposeFindingCommand,
    RecordImpactCommand,
    RequestToolRunCommand,
    command_summary,
)
from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    SecretRedactor,
    ToolExecutionError,
    ToolNotFoundError,
)
from samurai_ai.domain.genes import GeneStatus
from samurai_ai.domain.impact import ImpactError
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
from samurai_ai.services.genes import GeneService
from samurai_ai.services.impact import ImpactService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.questions import (
    OperatorQuestionError,
    OperatorQuestionService,
)
from samurai_ai.services.skills import SkillService
from samurai_ai.storage.agents import AgentRunRepository

_T = TypeVar("_T")

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_DEPTH = 1
MAX_CONSECUTIVE_REJECTS = 3
#: the same command repeated this often in one run earns a model note
REPEAT_THRESHOLD = 3
#: the same *place* called this often inside the window earns a note (a changing
#: payload still counts), and this often again after the note ends the run
#: a relayed command line (prlctl over ssh, a wrapper script) makes every call
#: look alike, so the window is wider than the plain-shell case needs
SITE_WINDOW = 16
SITE_NUDGE = 8
SITE_STOP = 14
#: parameters that name the place a tool acts on, with the payload left out
_SITE_KEYS = ("targets", "target", "host", "url", "path", "port", "asset")
#: a run cannot outlive this, whatever the steps do (overridable per call)
DEFAULT_MAX_WALL_SECONDS = 900.0


_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "exceeds the available context size",
    "reduce the length of the messages",
    "too many tokens",
)


def call_site(tool_id: str, params: dict[str, Any]) -> str:
    """The place a call acts on, with the payload left out.

    ``web_request(path=/nslookup.php, cmd=dir)`` and the same call with ``cmd=type
    C:\\boot.ini`` are one site: only a per-place window catches that loop, because
    every payload gives the argument-keyed detector a fresh signature.
    """
    where = {key: params[key] for key in _SITE_KEYS if key in params}
    return tool_id + "|" + json.dumps(where, sort_keys=True, default=str)


class SiteWindow:
    """Bounded memory of recent call sites for one run."""

    def __init__(self, window: int = SITE_WINDOW) -> None:
        self._seen: deque[str] = deque(maxlen=window)
        self._noted: set[str] = set()

    def record(self, site: str) -> tuple[bool, bool]:
        """Return (nudge, stop) after adding the site."""
        self._seen.append(site)
        hits = sum(1 for item in self._seen if item == site)
        if hits >= SITE_STOP and site in self._noted:
            return False, True
        if hits >= SITE_NUDGE and site not in self._noted:
            self._noted.add(site)
            return True, False
        return False, False


class ContextOverflowError(AgentError):
    """The model refused the prompt because it does not fit its window."""


def _is_overflow(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _OVERFLOW_MARKERS)


_MAX_BLACKBOARD_ITEMS = 20
#: blackboard items kept at each context-recovery level (0 = normal)
_CONTEXT_LEVELS = {0: _MAX_BLACKBOARD_ITEMS, 1: 6, 2: 2}
MAX_CONTEXT_RETRIES = 2
_CLOSE_OUT_ATTEMPTS = 2
_CLOSE_OUT_STEP = (
    "The planned steps are done. Emit exactly one command: complete_task if the task is "
    'finished ({"type":"complete_task"}), otherwise fail with the reason '
    '({"type":"fail","reason":...}). No tool runs.'
)

_PLANNER_INSTRUCTION = (
    "You are the planner of a governed security operation. Decompose the "
    "task into at most 4 concrete steps. Respond with exactly one JSON "
    'object: {"steps": ["step one", "step two"]}. No prose.'
)
_EXECUTOR_INSTRUCTION = (
    "You are the executor of a governed security operation. Prefer the dedicated "
    "adapters (recon.*, scan.*, fuzz.*, research.*) over shell.exec: they carry the "
    "parsing, the scope check and the egress record. Targets must be names the "
    "sandbox can resolve: an IP or a domain, not the application's own title "
    "(juice-shop does not resolve, 127.0.0.1 does). Values must be "
    "literal — a URL needs scheme and host (https://example.com/), and an angle "
    "bracket placeholder is copied as-is, never resolved. Emit exactly one "
    'JSON object: {"commands": [...]}. Allowed command types: '
    'request_tool_run {"type":"request_tool_run","tool_id":...,"params":{...}} '
    '(a rejected call costs a step: read the refusal and fix the value), '
    'note {"type":"note","key":...,"value":...}, ask_operator '
    '{"type":"ask_operator","questions":[{"id":"q1","question":...,"header":...,'
    '"options":[{"label":...,"description":...,"recommended":true},...]}],'
    '"timeout":120} — ask the operator and keep working if nobody answers, '
    'propose_finding '
    '{"type":"propose_finding","title":...,"summary":...,"severity":...,'
    '"artifact_ids":[...]}, complete_task {"type":"complete_task"}, fail '
    '{"type":"fail","reason":...}. No prose.'
)


def _repeat_block(notes: list[str]) -> str:
    """A nudge when the run keeps making the same call."""
    listed = "; ".join(dict.fromkeys(notes))
    return f"[repeats] same call again: {listed}\n"


def _rejection_block(reasons: list[str]) -> str:
    """What the model must read before trying again, in its own prompt."""
    listed = "\n".join(f"- {reason}" for reason in dict.fromkeys(reasons))
    return f"[rejected] Your previous commands were refused:\n{listed}\nFix them.\n"


def _parameter_hint(parameters: dict[str, Any]) -> str:
    """Name the accepted parameters, required ones first.

    Without this the model invents keys and the registry rejects the call, which
    costs a whole round for something the prompt could have said.
    """
    required = parameters.get("required") or ()
    properties = parameters.get("properties") or {}
    if not required and not properties:
        return " (no parameters)"
    names = [str(name) for name in required]
    names += [name for name in properties if name not in names]
    parts = []
    for name in names:
        shape = properties.get(name) or {}
        label = f"{name}*" if name in required else name
        parts.append(f"{label}{_shape_hint(shape)}")
    return f" (params: {', '.join(parts)}; * required)"


def _shape_hint(shape: dict[str, Any]) -> str:
    """The type and, crucially, the allowed values — a guess costs a round."""
    kind = shape.get("type")
    allowed = shape.get("enum")
    if allowed:
        return f":{kind} one of {'|'.join(str(item) for item in allowed)}"
    if kind == "array":
        items = (shape.get("items") or {}).get("type")
        return f":{kind}<{items}>" if items else f":{kind}"
    return f":{kind}" if kind else ""


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
        genes: GeneService | None = None,
        impact: ImpactService | None = None,
        questions: OperatorQuestionService | None = None,
    ) -> None:
        self.models = models
        self.kernel = kernel
        self.execution = execution
        self.operations = operations
        self.runs = runs
        self.redactor = redactor or SecretRedactor()
        self.memory = memory
        self.skills = skills
        self.genes = genes
        self.impact = impact
        self.questions = questions
        #: set by the host so a surfaced question can reach the operator
        self.question_notice: Callable[[str], None] | None = None

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
        max_wall_seconds: float | None = DEFAULT_MAX_WALL_SECONDS,
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
                max_wall_seconds=max_wall_seconds,
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
            fragment, skill_ids = self.skills.prompt_fragment(skill_names, workspace)
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
            f"{self._genes_excerpt()}"
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

    def _deadline_hit(self, run: AgentRun, deadline_at: float | None) -> bool:
        if deadline_at is None or time.monotonic() <= deadline_at:
            return False
        self._finish_failed(run, AgentRunStatus.FAILED, "wall deadline reached")
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
        max_wall_seconds: float | None = DEFAULT_MAX_WALL_SECONDS,
    ) -> None:
        consecutive_rejects = 0
        deadline_at = (
            None if max_wall_seconds is None else time.monotonic() + max_wall_seconds
        )
        repeat_counts: dict[str, int] = {}
        repeat_notes: list[str] = []
        sites = SiteWindow()
        for index, step in enumerate(steps):
            if run.status is not AgentRunStatus.RUNNING:
                return
            if self._stop_requested(run, stop_event) or self._deadline_hit(
                run, deadline_at
            ):
                return
            if repeat_notes:
                extra_context = _repeat_block(repeat_notes) + extra_context
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
            rejects, consecutive_rejects = self._run_commands(
                operation_id,
                run,
                commands,
                consecutive_rejects,
                read_only_only=read_only_only,
                approval_resolver=approval_resolver,
                stop_event=stop_event,
                repeat_counts=repeat_counts,
                repeat_notes=repeat_notes,
                sites=sites,
            )
            if run.status is not AgentRunStatus.RUNNING:
                return
            if run.status is not AgentRunStatus.RUNNING:
                # the site window can end the run from inside the command loop
                return
            if rejects and not self._stop_requested(run, stop_event):
                # the model never learns why a command was refused, so it repeats
                # it; one re-ask per step, with the reasons, is the whole remedy
                retry = self._execute_step_prompt(
                    operation_id,
                    run,
                    step,
                    index,
                    approval_id,
                    extra_context,
                    approval_resolver=approval_resolver,
                    model_approvals=model_approvals,
                    stop_event=stop_event,
                    rejection_notes=_rejection_block(rejects),
                )
                _rejects, consecutive_rejects = self._run_commands(
                    operation_id,
                    run,
                    retry,
                    consecutive_rejects,
                    read_only_only=read_only_only,
                    approval_resolver=approval_resolver,
                    stop_event=stop_event,
                    repeat_counts=repeat_counts,
                    repeat_notes=repeat_notes,
                    sites=sites,
                )
                if run.status is not AgentRunStatus.RUNNING:
                    return
        # the planned steps ran out while the task was still open: one forced
        # closing step, or the work silently evaporates with the run
        still_open = self._task_open(operation_id, run.task_id)
        if still_open and not self._stop_requested(run, stop_event):
            self._close_out(
                operation_id,
                run,
                approval_id,
                extra_context,
                approval_resolver=approval_resolver,
                model_approvals=model_approvals,
                stop_event=stop_event,
            )

    def _run_commands(
        self,
        operation_id: str,
        run: AgentRun,
        commands: tuple[AgentCommand, ...],
        consecutive_rejects: int,
        *,
        read_only_only: bool,
        approval_resolver: Callable[[str], bool] | None,
        stop_event: threading.Event | None,
        repeat_counts: dict[str, int] | None = None,
        repeat_notes: list[str] | None = None,
        sites: SiteWindow | None = None,
    ) -> tuple[list[str], int]:
        rejects: list[str] = []
        applied = 0
        for command in commands:
            if self._stop_requested(run, stop_event):
                return rejects, consecutive_rejects
            if repeat_counts is not None:
                self._note_repeat(run, command, repeat_counts, repeat_notes)
            reason = self._apply_command(
                operation_id,
                run,
                command,
                read_only_only=read_only_only,
                approval_resolver=approval_resolver,
                stop_event=stop_event,
            )
            if reason is None:
                consecutive_rejects = 0
                applied += 1
                if sites is not None and isinstance(command, RequestToolRunCommand):
                    self._note_site(run, command, sites, repeat_notes)
                continue
            rejects.append(reason)
            if approval_resolver is not None and reason.startswith("approval-required"):
                # with a resolver in place an approval miss is a bookkeeping
                # step between two real attempts, not a model mistake
                continue
            consecutive_rejects += 1
            if consecutive_rejects > MAX_CONSECUTIVE_REJECTS:
                raise AgentError(
                    f"too many consecutive rejected commands (>{MAX_CONSECUTIVE_REJECTS})"
                )
            if run.status is not AgentRunStatus.RUNNING:
                return rejects, consecutive_rejects
        if applied:
            # a step that got something done does not need re-asking
            return [], consecutive_rejects
        return rejects, consecutive_rejects

    def _note_site(
        self,
        run: AgentRun,
        command: RequestToolRunCommand,
        sites: SiteWindow,
        notes: list[str] | None,
    ) -> None:
        """Same tool, same place, new payload: the loop the arg-keyed matcher misses."""
        site = call_site(command.tool_id, command.params)
        nudge, stop = sites.record(site)
        if nudge:
            self._append(
                run.operation_id,
                "agent.command.same-site",
                {"run_id": run.id, "tool_id": command.tool_id, "site": site[:200]},
            )
            if notes is not None:
                notes.append(
                    f"{command.tool_id} уже {SITE_NUDGE} раз в одно и то же место с "
                    "разными данными — меняй место, а не payload, или закрывай шаг"
                )
        if stop:
            self._finish_failed(
                run, AgentRunStatus.FAILED, "same site loop: changing payload, same place"
            )

    def _note_repeat(
        self,
        run: AgentRun,
        command: AgentCommand,
        counts: dict[str, int],
        notes: list[str] | None,
    ) -> None:
        """The same tool call over and over earns a note, once per command."""
        key = f"{command.type}:{repr(sorted(command.model_dump().items()))}"
        counts[key] = counts.get(key, 0) + 1
        if counts[key] != REPEAT_THRESHOLD:
            return
        described = command.model_dump().get("tool_id") or command.type
        self.operations.repository.append_event(
            OperationEvent(
                operation_id=run.operation_id,
                event_type="agent.command.repeated",
                payload={"run_id": run.id, "command": described, "times": counts[key]},
            )
        )
        if notes is not None:
            notes.append(
                f"{described} уже вызывался {counts[key]} раза с теми же "
                "параметрами — смени параметры или инструмент"
            )

    def _task_done(self, operation_id: str, task_id: str) -> bool:
        plan = self.kernel.get_current_plan(operation_id)
        task = plan.task_by_id(task_id) if plan is not None else None
        return task is not None and task.status is TaskStatus.DONE

    def _task_open(self, operation_id: str, task_id: str) -> bool:
        plan = self.kernel.get_current_plan(operation_id)
        task = plan.task_by_id(task_id) if plan is not None else None
        return task is not None and task.status is TaskStatus.IN_PROGRESS

    def _close_out(
        self,
        operation_id: str,
        run: AgentRun,
        approval_id: str | None,
        extra_context: str,
        *,
        approval_resolver: Callable[[str], bool] | None,
        model_approvals: dict[str, str] | None,
        stop_event: threading.Event | None,
    ) -> None:
        """Ask for the closing command alone when the plan ran out mid-task."""
        notes = ""
        for _attempt in range(_CLOSE_OUT_ATTEMPTS):
            try:
                commands = self._execute_step_prompt(
                    operation_id,
                    run,
                    _CLOSE_OUT_STEP,
                    len(_CLOSE_OUT_STEP),
                    approval_id,
                    extra_context,
                    approval_resolver=approval_resolver,
                    model_approvals=model_approvals,
                    stop_event=stop_event,
                    rejection_notes=notes,
                )
            except AgentError:
                # a failed closing step must not discard what the run already did
                return
            # a `fail` next to a tool call usually blames the tool, not the task
            if self._task_open(operation_id, run.task_id) and any(
                isinstance(command, RequestToolRunCommand) for command in commands
            ):
                commands = tuple(
                    command for command in commands if not isinstance(command, FailCommand)
                )
            reasons = self._apply_closing(operation_id, run, commands, approval_resolver)
            if not self._task_open(operation_id, run.task_id) or not reasons:
                return
            notes = _rejection_block(reasons)

    def _apply_closing(
        self,
        operation_id: str,
        run: AgentRun,
        commands: tuple[AgentCommand, ...],
        approval_resolver: Callable[[str], bool] | None,
    ) -> list[str]:
        reasons: list[str] = []
        for command in commands:
            if not isinstance(command, (RequestToolRunCommand, CompleteTaskCommand)):
                continue
            reason = self._apply_command(
                operation_id,
                run,
                command,
                read_only_only=False,
                approval_resolver=approval_resolver,
            )
            if reason is not None:
                reasons.append(reason)
        return reasons

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
        rejection_notes: str = "",
    ) -> tuple[AgentCommand, ...]:
        def build(level: int) -> str:
            """The step prompt at a context level; higher levels carry less context."""
            blackboard = Blackboard.from_events(self.operations.events(operation_id))
            excerpt = self._blackboard_excerpt(
                blackboard, run.id, _CONTEXT_LEVELS.get(level, 2)
            )
            genes = self._genes_excerpt() if level < 1 else ""
            tools = self._tools_excerpt() if level < 2 else ""
            prompt = (
                f"{_EXECUTOR_INSTRUCTION}{profile_for(run.specialization).executor_focus}\n"
                f"{genes}"
                f"{tools}\n"
                f"{rejection_notes}"
                f"Current step: {step}\n"
                f"Blackboard:\n{excerpt or '(empty)'}"
            )
            if extra_context:
                prompt += f"\n\n{extra_context}"
            if level:
                prompt += (
                    "\n\n[context trimmed to fit the window: "
                    f"level {level}, blackboard keeps {_CONTEXT_LEVELS.get(level, 2)} "
                    "entries — re-read the step if detail is missing]"
                )
            return prompt

        commands = self._call_model(
            operation_id,
            run,
            build(0),
            parse_executor_output,
            approval_id,
            approval_resolver=approval_resolver,
            model_approvals=model_approvals,
            stop_event=stop_event,
            rebuild=build,
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
        rebuild: Callable[[int], str] | None = None,
    ) -> _T:
        current = prompt
        level = 0
        overflows = 0
        for attempt in range(MAX_PARSE_RETRIES + 1):
            try:
                response = self._complete(
                    operation_id,
                    run,
                    current,
                    approval_id,
                    approval_resolver=approval_resolver,
                    model_approvals=model_approvals,
                    stop_event=stop_event,
                )
            except ContextOverflowError as exc:
                # shrinking beats dying: fewer blackboard entries, no optional blocks,
                # and a marker so the model knows detail was cut rather than absent
                overflows += 1
                self._append(
                    operation_id,
                    "agent.context.overflow",
                    {"run_id": run.id, "level": level, "detail": str(exc)[:200]},
                )
                if rebuild is None or overflows > MAX_CONTEXT_RETRIES:
                    self._append(
                        operation_id,
                        "agent.context.exhausted",
                        {"run_id": run.id, "attempts": overflows},
                    )
                    raise AgentError(
                        "the prompt does not fit the model context window even after "
                        "trimming; shorten the step or use a model with a larger window"
                    ) from exc
                level = overflows
                current = rebuild(level)
                self._append(
                    operation_id,
                    "agent.context.recovered",
                    {"run_id": run.id, "level": level},
                )
                continue
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
            except ModelError as exc:
                if _is_overflow(str(exc)):
                    raise ContextOverflowError(str(exc)) from exc
                raise
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
        stop_event: threading.Event | None = None,
    ) -> str | None:
        """None when the command was applied, the reason when it was rejected."""
        try:
            if isinstance(command, RequestToolRunCommand):
                if read_only_only and not self._is_read_only(command.tool_id):
                    reason = "plan mode allows read-only tool calls only"
                    self._reject(operation_id, run, command, reason)
                    return reason
                self._command_tool_run(
                    operation_id, run, command, approval_resolver=approval_resolver
                )
            elif isinstance(command, NoteCommand):
                try:
                    self._append_note(operation_id, run, command)
                except ValueError as exc:
                    reason = f"invalid note key: {exc}"
                    self._reject(operation_id, run, command, reason)
                    return reason
            elif isinstance(command, ProposeFindingCommand):
                self._command_propose_finding(operation_id, run, command)
            elif isinstance(command, RecordImpactCommand):
                self._command_record_impact(run, command)
            elif isinstance(command, AskOperatorCommand):
                self._command_ask_operator(operation_id, run, command, stop_event)
            elif isinstance(command, CompleteTaskCommand):
                # re-affirming completion is not a mistake worth a refusal
                if self._task_done(operation_id, run.task_id):
                    return None
                self.kernel.complete_task(operation_id, run.task_id)
            elif isinstance(command, FailCommand):
                raise AgentError(f"agent requested failure: {command.reason}")
            else:
                raise AgentError(f"unsupported command type: {command!r}")
        except ApprovalRequiredError as exc:
            reason = f"approval-required:{exc.approval_id}"
            self._reject(operation_id, run, command, reason)
            return reason
        except (ToolNotFoundError, ToolExecutionError) as exc:
            self._reject(operation_id, run, command, str(exc))
            return str(exc)
        except (KernelError, ArtifactError) as exc:
            self._reject(operation_id, run, command, str(exc))
            return str(exc)
        except ImpactError as exc:
            self._reject(operation_id, run, command, str(exc))
            return str(exc)
        return None

    def _command_ask_operator(
        self,
        operation_id: str,
        run: AgentRun,
        command: AskOperatorCommand,
        stop_event: threading.Event | None,
    ) -> None:
        """Ask the operator; the answer reaches the next step as a note."""
        if self.questions is None:
            raise AgentError("operator questions are not wired into this harness")
        try:
            outcome = self.questions.ask(
                operation_id,
                run.id,
                command,
                notify=self.question_notice,
                stop_event=stop_event,
            )
        except OperatorQuestionError as exc:
            raise AgentError(str(exc)) from exc
        key = "operator-question" if outcome.pending else "operator-answer"
        self._append_note(
            operation_id,
            run,
            NoteCommand(type="note", key=f"{key}.{outcome.call_id}", value=outcome.text),
        )

    def _command_record_impact(self, run: AgentRun, command: RecordImpactCommand) -> None:
        """The model escalates impact itself; the ladder decides what counts."""
        if self.impact is None:
            raise AgentError("impact escalation is not wired into this harness")
        self.impact.record(
            command.finding_id,
            command.level,
            tuple(command.evidence),
            rollback=command.rollback,
            outcome=command.outcome,
        )
        self._append(
            run.operation_id,
            "agent.command.impact",
            {
                "run_id": run.id,
                "finding_id": command.finding_id,
                "level": command.level,
                "outcome": command.outcome,
            },
        )

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
            # the artifact ids ride along: a finding must name evidence, and the
            # model otherwise reaches for the note key instead of the artifact
            evidence = (
                f"\nartifact_ids: {', '.join(result.artifact_ids)}"
                if result.artifact_ids
                else ""
            )
            self._append_note(
                operation_id,
                run,
                NoteCommand(
                    type="note",
                    key=f"tool-result.{command.tool_id}",
                    value=f"{excerpt}{evidence}",
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

    def _genes_excerpt(self) -> str:
        """Active genes are the evolved part of the prompt, above the tools.

        Only `active` genes get here: `shadow` ones exist but prove nothing yet.
        """
        if self.genes is None:
            return ""
        blocks = [
            f"- [{gene.slot}] {gene.body}" for gene in self.genes.list_genes(GeneStatus.ACTIVE)
        ]
        if not blocks:
            return ""
        return "[evolved hints]\n" + "\n".join(blocks) + "\n\n"

    def _tools_excerpt(self) -> str:
        lines = [
            "Available tools — reachable only through the request_tool_run "
            "command, with the id in tool_id and these parameters as its only keys "
            "(required ones must be present):",
        ]
        for definition in self.execution.list_tools():
            description = definition.description.split(".")[0][:80]
            lines.append(
                f"- {definition.id} (risk {definition.risk_class.value}): {description}"
                f"{_parameter_hint(definition.parameters)}"
            )
        return "\n".join(lines)

    def _blackboard_excerpt(
        self, blackboard: Blackboard, run_id: str, limit: int = _MAX_BLACKBOARD_ITEMS
    ) -> str:
        visible = blackboard.visible_for(run_id)
        lines = [f"{key}={value}" for key, value in list(visible.items())[:limit]]
        return "\n".join(lines)

    def _append(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self._append_event(
            OperationEvent(
                operation_id=operation_id, event_type=event_type, payload=payload
            )
        )

    def _append_event(self, event: OperationEvent) -> None:
        self.operations.repository.append_event(event)
