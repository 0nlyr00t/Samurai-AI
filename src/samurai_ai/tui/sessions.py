"""Subagent child sessions: spawn, watch, stop.

A child runs one plan task in its own thread under the governed orchestrator,
so the parent turn never blocks. It cannot outrun the operator: approvals come
back to the UI thread as a prompt, the stop event is checked between steps, and
plan mode's read-only rule is forwarded as ``read_only_only``.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from samurai_ai.domain.agents import AgentError, AgentPlanError, AgentRunStatus
from samurai_ai.domain.kernel import PlanTask, RiskClass, StageGate, TaskStatus
from samurai_ai.domain.swarm import AgentSpecialization

if TYPE_CHECKING:  # pragma: no cover - typing only
    from samurai_ai.domain.agents import AgentRun
    from samurai_ai.domain.operations import Operation


@dataclass
class _Spawn:
    """Handle of the running child; ``run_id`` lands once the row exists."""

    task_id: str
    instruction: str
    run_id: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)


class SessionController:
    """Mixin: the app's subagent surface (spawn, stop, approval bridge)."""

    if TYPE_CHECKING:  # pragma: no cover - typing only
        operation: Operation | None
        screen: Any
        orchestrator: Any
        kernel: Any
        settings: Any
        models: Any
        translator: Any
        _plan_mode: bool
        _permission_mode: Any

        def _write_line(self, message: Any) -> None: ...
        def _write_dim(self, message: str) -> None: ...
        def _refresh_chrome(self) -> None: ...
        def run_worker(self, *args: Any, **kwargs: Any) -> Any: ...

        # App methods with generic signatures: typed loosely so the mixin fits
        push_screen: Any
        call_from_thread: Any

    _spawn: _Spawn | None = None
    _approval_wait: tuple[dict[str, bool], threading.Event] | None = None

    def _active_spawn(self, operation_id: str) -> _Spawn | None:
        spawn = cast("_Spawn | None", getattr(self, "_spawn", None))
        if spawn is None or not spawn.run_id:
            return None
        run = self.orchestrator.runs.get(spawn.run_id)
        if run is None or run.status is not AgentRunStatus.RUNNING:
            return None
        if run.operation_id != operation_id:
            return None
        return spawn

    # -- spawn ------------------------------------------------------------
    def _task_for_instruction(self, operation_id: str, instruction: str) -> str:
        """Reuse a free plan task, or bootstrap a one-task plan."""
        plan = self.kernel.get_current_plan(operation_id)
        if plan is not None:
            for task in plan.tasks:
                if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                    return str(task.id)
            raise AgentPlanError(self.translator.t("subagent.no-free-task"))

        task = PlanTask(
            id=f"sub-{uuid4().hex[:8]}",
            title=instruction[:200],
            risk_class=RiskClass.R2,
        )
        self.kernel.create_plan(
            operation_id,
            self.translator.t("subagent.plan-title"),
            [task],
            [StageGate(id=f"gate-{uuid4().hex[:6]}", name=instruction[:200])],
        )
        return task.id

    async def spawn_subagent(
        self,
        instruction: str,
        *,
        specialization: AgentSpecialization = AgentSpecialization.GENERAL,
    ) -> str | None:
        """Start a child session; returns the created run id, or None."""
        operation = self.operation
        if operation is None:
            self._write_line("[subagent] " + self.translator.t("status.no-operation"))
            return None
        instruction = instruction.strip()
        if not instruction:
            self._write_line("[subagent] " + self.translator.t("subagent.empty-instruction"))
            return None
        active = self._active_spawn(operation.id)
        if active is not None:
            self._write_line("[subagent] " + self.translator.t("subagent.busy", run=active.run_id))
            return None
        try:
            task_id = self._task_for_instruction(operation.id, instruction)
        except (AgentPlanError, AgentError) as exc:
            self._write_line(f"[subagent] {exc}")
            return None

        spawn = _Spawn(task_id=task_id, instruction=instruction)
        started = threading.Event()
        self._spawn = spawn
        self.run_worker(
            self._run_spawn(operation.id, spawn, specialization, started),
            exclusive=False,
        )
        await asyncio.to_thread(started.wait, 30)
        return spawn.run_id or None

    async def _run_spawn(
        self,
        operation_id: str,
        spawn: _Spawn,
        specialization: AgentSpecialization,
        started: threading.Event,
    ) -> None:
        model_id = self.models.default_model()
        if not model_id:
            started.set()
            self._session_finished(spawn, None, self.translator.t("subagent.no-model"))
            return

        def on_start(run: AgentRun) -> None:
            spawn.run_id = run.id
            self.call_from_thread(self._session_started, spawn)
            started.set()

        try:
            run = await asyncio.to_thread(
                self.orchestrator.run_task,
                operation_id,
                spawn.task_id,
                [model_id],
                read_only_only=bool(getattr(self, "_plan_mode", False)),
                stop_event=spawn.stop_event,
                approval_resolver=self._resolve_approval_from_thread,
                on_start=on_start,
                created_by="subagent",
                max_steps=self._session_budget(),
                specialization=specialization,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            started.set()
            self._session_finished(spawn, None, self._spawn_error_text(exc))
            return
        self._session_finished(spawn, run, "")

    def _session_started(self, spawn: _Spawn) -> None:
        self._write_dim(self.translator.t("subagent.started", run=spawn.run_id, task=spawn.task_id))
        self._refresh_chrome()

    def _session_finished(self, spawn: _Spawn, run: AgentRun | None, error: str) -> None:
        if getattr(self, "_spawn", None) is spawn:
            self._spawn = None
        if run is None:
            self._write_line(f"[subagent] {error}")
        else:
            key = (
                "subagent.finished"
                if run.status is AgentRunStatus.COMPLETED
                else "subagent.stopped"
            )
            self._write_line(self.translator.t(key, run=run.id, status=run.status.value))
        self._refresh_chrome()

    # -- stop -------------------------------------------------------------
    def stop_subagent(self, run_id: str | None = None) -> bool:
        spawn = cast("_Spawn | None", getattr(self, "_spawn", None))
        if spawn is None or (run_id is not None and spawn.run_id != run_id):
            return False
        spawn.stop_event.set()
        # A child blocked on an approval is not between steps, so the event
        # alone would leave it waiting: answer the prompt negatively and close
        # it. The run then ends as cancelled instead of hanging.
        waiting = self._approval_wait
        if waiting is not None:
            holder, answered = waiting
            holder["allowed"] = False
            answered.set()
            from samurai_ai.tui.screens import AccessPromptScreen

            if isinstance(self.screen, AccessPromptScreen):
                self.screen.dismiss(False)
        # The task goes back to pending right away so it can be replayed; a run
        # that finishes first has already set it DONE and the reset is skipped.
        self.release_task(spawn.task_id)
        return True

    def release_task(self, task_id: str) -> None:
        if self.operation is None:
            return
        try:
            self.kernel.reset_task(self.operation.id, task_id)
        except Exception:  # noqa: BLE001 - the task already moved on
            pass

    # -- approvals --------------------------------------------------------
    def _resolve_approval_from_thread(self, approval_id: str) -> bool:
        """Called on the child thread: ask the operator, block for the answer.

        ``yolo`` and ``never`` never ask, so a child's calls are approved in
        place, exactly like the ones from the chat.
        """
        if not self._permission_mode.prompts_for_tools:
            return True
        holder: dict[str, bool] = {}
        answered = threading.Event()
        spawn = cast("_Spawn | None", getattr(self, "_spawn", None))

        def ask() -> None:
            from samurai_ai.tui.screens import AccessPromptScreen

            subtitle = f"tool.run · {approval_id}"
            if spawn is not None and spawn.run_id:
                subtitle = f"{spawn.run_id} · {spawn.task_id} · {approval_id}"

            def decided(allowed: bool | None) -> None:
                holder["allowed"] = bool(allowed)
                answered.set()

            self.push_screen(AccessPromptScreen(self.translator, "tool.run", subtitle), decided)

        self._approval_wait = (holder, answered)
        try:
            self.call_from_thread(ask)
            answered.wait(timeout=600)
        finally:
            self._approval_wait = None
        return holder.get("allowed", False)

    # -- helpers ----------------------------------------------------------
    def child_runs(self) -> list[AgentRun]:
        if self.operation is None:
            return []
        return [
            run
            for run in self.orchestrator.list_runs(self.operation.id)
            if run.depth > 0 or run.created_by == "subagent"
        ]

    def _session_budget(self) -> int:
        value = self.settings.get_value("subagent.max_steps")
        try:
            return int(value) if value is not None else 12
        except (TypeError, ValueError):
            return 12

    def _spawn_error_text(self, exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"
