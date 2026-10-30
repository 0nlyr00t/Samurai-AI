"""Chat side of the TUI: the agent turn loop, its gates and its controls.

Kept as a mixin so the loop can use the app's widgets and services directly
while ``app.py`` stays a shell: layout, bindings, status bar, command dispatch.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.widgets import Input, Static

from samurai_ai import __version__
from samurai_ai.agents.provider import ChatResponse, Message, ToolCall, ToolSpec
from samurai_ai.domain.execution import ApprovalRequiredError
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.permission import classify_command
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization
from samurai_ai.tui.screens import AccessPromptScreen

if TYPE_CHECKING:
    from samurai_ai.i18n import Translator
    from samurai_ai.services.execution import ToolExecutionService
    from samurai_ai.services.goals import GoalService
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.models import ModelService
    from samurai_ai.services.operations import OperationService
    from samurai_ai.services.settings import SettingsService
    from samurai_ai.storage.models import CustomModelConflictError  # noqa: F401


class ChatController:
    """Behaviour that needs the app's widgets; mixed into ``SamuraiApp``."""

    if TYPE_CHECKING:
        service: OperationService
        kernel: KernelService
        settings: SettingsService
        execution: ToolExecutionService
        goals: GoalService
        models: ModelService
        translator: Translator
        operation: Operation | None
        _cwd: Any

        def paint(self, style: str) -> str: ...
        _chat_history: list[Message]
        _steer_queue: list[str]
        _chat_busy: bool
        _shell_running: str
        _shell_background: bool
        _ctx_tokens: int
        _expandable: list[tuple[str, int | None, str, str]]
        _last_ai_text: str | None
        _last_ai_code: str | None
        _thinking_timer: Any
        _thinking_depth: int
        _thinking_frame: int
        _plan_mode: bool
        _auto_title: str | None
        # properties on the app, so declared loosely here
        _permission_mode: Any
        _yolo: Any

        def _write_line(self, text: str) -> None: ...
        def _write_dim(self, text: str) -> None: ...
        def _write_user_message(self, text: str) -> None: ...
        def _write_ai_message(self, response: Any) -> None: ...
        def _refresh_chrome(self) -> None: ...
        def copy_to_clipboard(self, text: str) -> None: ...
        def query_one(self, *args: Any, **kwargs: Any) -> Any: ...
        def set_interval(self, *args: Any, **kwargs: Any) -> Any: ...
        def _directory_trusted(self) -> bool: ...
        def _tool_is_read_only(self, tool_id: str) -> bool: ...
        def spawn_subagent(self, instruction: str, **kwargs: Any) -> Any: ...
        def _reusable_approval(self, tool_id: str) -> bool: ...
        def _drain_steering(self) -> None: ...
        def _count_goal_step(self) -> None: ...
        def run_worker(self, *args: Any, **kwargs: Any) -> Any: ...
        def push_screen_wait(self, screen: Any) -> Any: ...

    def action_steer(self) -> None:
        """Ctrl-S: inject into the running turn, or send when nothing runs."""
        command_input = self.query_one("#command-input", Input)
        text = command_input.value.strip()
        if not text or not self._chat_busy:
            if text:
                command_input.value = ""
                command_input.post_message(Input.Submitted(command_input, text))
            return
        self._steer_queue.append(text)
        command_input.value = ""
        self._write_dim("↳ " + self.translator.t("chat.steer", text=text))

    # --- AI chat ---------------------------------------------------------

    _CHAT_MAX_TOOL_ROUNDS = 8
    _CHAT_HISTORY_LIMIT = 40
    _TOOL_OUTPUT_FEEDBACK_LIMIT = 4000

    _CHAT_TOOL_SPECS: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="shell_exec",
            description=(
                "Run a shell command on the operator's host via bash -lc. "
                "Returns the exit code plus bounded stdout and stderr. Use it "
                "to inspect the system, run security tooling and verify facts "
                "instead of guessing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="agent_spawn",
            description=(
                "Hand one plan task to a subagent child session that works in "
                "the background. Prefer it for a self-contained chunk (a scan, "
                "a set of checks) while you keep going. One child runs at a "
                "time; the operator can watch and stop it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 8192,
                    },
                    "specialization": {
                        "type": "string",
                        "enum": ["general", "recon", "testing", "analysis"],
                    },
                },
                "required": ["instruction"],
                "additionalProperties": False,
            },
        ),
    )
    _CHAT_TOOL_IDS = {"shell_exec": "shell.exec"}
    _CHAT_TOOL_HANDLERS = {
        "shell_exec": "_run_shell_tool",
        "agent_spawn": "_run_spawn_tool",
    }

    _THINKING_FRAMES = "⠋⠙⠹⠸⠼⴦⦧⡇⠏"
    _THINKING_COLORS = ("accent-alt", "accent", "accent", "accent")

    def _chat_system_prompt(self) -> str:
        autonomy = self.translator.t(
            "chat.autonomy.never"
            if not self._permission_mode.allows_operator_questions
            else "chat.autonomy.ask"
        )
        return self.translator.t(
            "chat.system",
            cwd=str(self._cwd),
            version=__version__,
            autonomy=autonomy,
        )

    def _trim_chat_history(self) -> None:
        if len(self._chat_history) > self._CHAT_HISTORY_LIMIT + 1:
            self._chat_history = (
                self._chat_history[:1] + self._chat_history[-self._CHAT_HISTORY_LIMIT :]
            )

    def _thinking_start(self) -> None:
        self._thinking_depth += 1
        widget = self.query_one("#thinking", Static)
        widget.display = True
        if self._thinking_timer is None:
            self._thinking_frame = 0
            self._thinking_tick()
            self._thinking_timer = self.set_interval(0.12, self._thinking_tick)

    def _thinking_tick(self) -> None:
        widget = self.query_one("#thinking", Static)
        frame = self._THINKING_FRAMES[self._thinking_frame % len(self._THINKING_FRAMES)]
        dots = "·" * (1 + (self._thinking_frame // 4) % 3)
        color = self._THINKING_COLORS[(self._thinking_frame // 8) % len(self._THINKING_COLORS)]
        widget.update(
            Text(
                f"{frame} {self.translator.t('chat.thinking')}{dots}",
                style=self.paint(f"bold {color}"),
            )
        )
        self._thinking_frame += 1

    def _thinking_stop(self) -> None:
        self._thinking_depth = max(0, self._thinking_depth - 1)
        if self._thinking_depth:
            return
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        self.query_one("#thinking", Static).display = False

    def _start_chat(self, text: str) -> None:
        model_id = self.models.default_model()
        if model_id is None:
            self._write_line("[error] " + self.translator.t("chat.no-model"))
            return
        title = " ".join(text.split()[:6])
        if not self.operation:
            # No ceremony: the first message spawns its own operation tab.
            self.operation = self.service.create(
                title or "chat",
                OperationMode.BUG_BOUNTY,
                ScopeContract(includes=("127.0.0.1/32",)),
            )
        if self._auto_title is None:
            # The tab names itself from the task at hand
            self._auto_title = title
        self._expandable.clear()
        self._write_user_message(text)
        self._thinking_start()
        self.run_worker(self._chat(text, model_id, None), exclusive=False)

    def _shell_mode(self, command: str) -> None:
        """`!cmd`: run on the host without spending a model round."""
        if not command:
            self._write_dim(self.translator.t("shell.empty"))
            return
        self.run_worker(self._run_shell_mode(command), exclusive=False)

    def action_shell_background(self) -> None:
        if not self._shell_running:
            return
        self._shell_background = True
        self._write_dim(self.translator.t("shell.background", command=self._shell_running))

    def action_expand_output(self) -> None:
        self._expand_outputs()

    async def _run_shell_mode(self, command: str) -> None:
        operation = self.operation
        if operation is None:
            self.operation = self.service.create(
                "shell",
                OperationMode.BUG_BOUNTY,
                ScopeContract(includes=("127.0.0.1/32",)),
            )
            operation = self.operation
        self._shell_running = command
        self._shell_background = False
        # Shell-mode output lands in the conversation context, so it is marked
        # as untrusted-external exactly like a tool result.
        self._chat_history.append(
            Message(role="user", content=f"[shell !{command}] (operator command)")
        )
        risk = classify_command(command, str(self._cwd))
        badge = f"  [{risk.label()}]" if not risk.empty else ""
        self._write_dim(f"⚙ !{command}{badge}")
        mode = self._permission_mode
        ask = mode.prompts_for_tools and (
            not self._reusable_approval("shell.exec") or not risk.empty
        )
        if ask:
            allowed = await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    f"shell.exec: {command}",
                    f"r3 · {risk.label()}" if not risk.empty else "r3",
                )
            )
            if not allowed:
                self._write_dim(self.translator.t("chat.tool-denied", command=command))
                self._chat_history.append(
                    Message(role="user", content=f"[shell !{command}] denied by the operator")
                )
                self._shell_running = ""
                return
        approval_id: str | None = None
        try:
            try:
                result = await asyncio.to_thread(
                    self.execution.run_tool, operation.id, "shell.exec", {"command": command}
                )
            except ApprovalRequiredError as exc:
                self.kernel.approve(
                    exc.approval_id,
                    decided_by="operator" if ask else mode.value,
                )
                approval_id = exc.approval_id
                result = await asyncio.to_thread(
                    self.execution.run_tool,
                    operation.id,
                    "shell.exec",
                    {"command": command},
                    approval_id=approval_id,
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._write_line(f"[error] {exc}")
            self._chat_history.append(
                Message(role="user", content=f"[shell !{command}] error: {exc}")
            )
            self._shell_running = ""
            return
        prefix = "[bg] " if self._shell_background else ""
        self._expandable.append((f"!{command}", result.run.exit_code, result.stdout, result.stderr))
        self._write_dim(
            prefix
            + self.translator.t(
                "chat.tool-collapsed",
                command=f"!{command}",
                code=result.run.exit_code,
                lines=len(result.stdout.strip().splitlines()),
            )
        )
        body = (result.stdout if result.stdout.strip() else result.stderr).rstrip()
        self._chat_history.append(
            Message(
                role="user",
                content=(
                    f"[shell !{command}] exit={result.run.exit_code}\n"
                    f"{body[: self._TOOL_OUTPUT_FEEDBACK_LIMIT]}"
                ),
            )
        )
        self._trim_chat_history()
        self._shell_running = ""

    async def _chat(self, text: str, model_id: str, approval_id: str | None) -> None:
        self._chat_busy = True
        try:
            await self._chat_loop(text, model_id, approval_id)
        finally:
            self._chat_busy = False
            self._shell_running = ""
            self._shell_background = False
            self._thinking_stop()
            self._refresh_chrome()
            self.query_one("#command-input", Input).focus()

    async def _request_chat_completion(
        self,
        operation_id: str,
        model_id: str,
        reasoning: str | None,
        tools: tuple[ToolSpec, ...],
    ) -> ChatResponse | None:
        """One model call with inline approval handling; None = denied/failed."""
        approval_id: str | None = None
        while True:
            try:
                return await asyncio.to_thread(
                    self.models.chat,
                    operation_id,
                    model_id,
                    tuple(self._chat_history),
                    tools=tools,
                    reasoning_effort=reasoning,
                    approval_id=approval_id,
                )
            except ApprovalRequiredError as exc:
                approval = self.kernel.kernel.get_approval(exc.approval_id)
                action = approval.action if approval is not None else ""
                risk = approval.risk_class.value if approval is not None else "?"
                if self._yolo or action.startswith("model.call"):
                    # Model calls are covered by the directory trust granted at
                    # startup; only OS-level command execution still prompts.
                    decided_by = (
                        "auto" if action.startswith("model.call") else self._permission_mode.value
                    )
                    self.kernel.approve(exc.approval_id, decided_by=decided_by)
                    approval_id = exc.approval_id
                    continue
                allowed = await self.push_screen_wait(
                    AccessPromptScreen(self.translator, action, risk)
                )
                if allowed:
                    self.kernel.approve(exc.approval_id, decided_by="operator")
                    self._write_line(
                        "[allow] " + self.translator.t("access.granted", action=action)
                    )
                    approval_id = exc.approval_id
                    continue
                self.kernel.reject(exc.approval_id, decided_by="operator")
                self._write_line("[deny] " + self.translator.t("access.denied", action=action))
                return None

    async def _chat_loop(self, text: str, model_id: str, approval_id: str | None) -> None:
        del approval_id  # approvals are handled per call inside the loop
        reasoning = (
            self.settings.get_value(f"model.reasoning_effort.{model_id}")
            or self.models.default_reasoning_effort()
        )
        operation_id = self.operation.id if self.operation else ""
        if not self._chat_history:
            self._chat_history.append(Message(role="system", content=self._chat_system_prompt()))
        self._chat_history.append(Message(role="user", content=text))
        for _round in range(self._CHAT_MAX_TOOL_ROUNDS):
            self._drain_steering()
            self._count_goal_step()
            try:
                response = await self._request_chat_completion(
                    operation_id, model_id, reasoning, self._CHAT_TOOL_SPECS
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                self._write_line(f"[error] {exc}")
                return
            if response is None:
                return
            self._ctx_tokens += response.usage.prompt_tokens + response.usage.completion_tokens
            if not response.tool_calls:
                self._chat_history.append(Message(role="assistant", content=response.text))
                self._trim_chat_history()
                if response.text.strip():
                    self._write_ai_message(response)
                else:
                    self._write_dim(self.translator.t("chat.empty-reply"))
                if self._steer_queue:
                    # the operator steered while this answer was in flight
                    continue
                return
            if response.text.strip():
                self._chat_history.append(Message(role="assistant", content=response.text))
            for call in response.tool_calls:
                self._chat_history.append(await self._run_chat_tool(call))
            self._trim_chat_history()
        # The tool rounds are exhausted: force a final summary call without
        # tools so the operator always gets a closing answer instead of
        # silence after the loop.
        self._write_dim(self.translator.t("chat.tool-limit"))
        try:
            final = await self._request_chat_completion(operation_id, model_id, reasoning, ())
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._write_line(f"[error] {exc}")
            return
        if final is None:
            return
        self._ctx_tokens += final.usage.prompt_tokens + final.usage.completion_tokens
        self._chat_history.append(Message(role="assistant", content=final.text))
        self._trim_chat_history()
        if final.text.strip():
            self._write_ai_message(final)

    def _chat_tool_read_only(self, name: str) -> bool:
        tool_id = self._CHAT_TOOL_IDS.get(name)
        return tool_id is not None and self._tool_is_read_only(tool_id)

    async def _run_chat_tool(self, call: ToolCall) -> Message:
        handler = self._CHAT_TOOL_HANDLERS.get(call.name)
        if handler is None:
            return Message(
                role="user",
                content=f"[tool error] unknown tool: {call.name!r}",
            )
        if not self._directory_trusted():
            self._write_dim(self.translator.t("chat.tool-denied", command=call.name))
            return Message(
                role="user",
                content=f"[tool {call.name}] the operator has not trusted this directory",
            )
        result: Message = await getattr(self, handler)(call)
        return result

    async def _run_spawn_tool(self, call: ToolCall) -> Message:
        instruction = str(call.arguments.get("instruction", "")).strip()
        if not instruction:
            return Message(role="user", content="[tool agent_spawn] empty instruction")
        if self._plan_mode:
            self._write_dim(self.translator.t("plan.blocked", command=f"agent_spawn {instruction}"))
            return Message(
                role="user",
                content=("[tool agent_spawn] blocked: plan mode allows read-only calls only"),
            )
        raw = str(call.arguments.get("specialization", "") or "general")
        try:
            specialization = AgentSpecialization(raw)
        except ValueError:
            specialization = AgentSpecialization.GENERAL
        run_id = await self.spawn_subagent(instruction, specialization=specialization)
        if run_id is None:
            return Message(
                role="user",
                content="[tool agent_spawn] the child did not start (see the journal)",
            )
        self._write_dim(self.translator.t("subagent.tool-started", run=run_id))
        return Message(
            role="user",
            content=(
                f"[tool agent_spawn] run={run_id} task=<plan task> started in the "
                "background; its steps and output are in the sessions screen (Ctrl-G)"
            ),
        )

    async def _run_shell_tool(self, call: ToolCall) -> Message:
        tool_id = self._CHAT_TOOL_IDS.get(call.name)
        command = str(call.arguments.get("command", "")).strip()
        if tool_id is None or not command:
            return Message(
                role="user",
                content=f"[tool error] unknown tool or empty command: {call.name!r}",
            )
        if self._plan_mode and not self._tool_is_read_only(tool_id):
            self._write_dim(self.translator.t("plan.blocked", command=command))
            return Message(
                role="user",
                content=(
                    f"[tool {call.name}] blocked: plan mode allows read-only "
                    f"calls only (command: {command})"
                ),
            )
        risk = classify_command(command, str(self._cwd))
        badge = f"  [{risk.label()}]" if not risk.empty else ""
        self._write_dim(f"⚙ {command}{badge}")
        # A tool already approved for this operation is not asked again, except
        # for a sensitive path or a destructive shape, which stay worth a look.
        mode = self._permission_mode
        ask = mode.prompts_for_tools and (not self._reusable_approval(tool_id) or not risk.empty)
        if ask:
            allowed = await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    f"{tool_id}: {command}",
                    f"r3{badge}" if risk.empty else f"r3 · {risk.label()}",
                )
            )
            if not allowed:
                self._write_dim(self.translator.t("chat.tool-denied", command=command))
                return Message(
                    role="user",
                    content=f"[tool {call.name}] denied by the operator",
                )
        approval_id: str | None = None
        try:
            result = await asyncio.to_thread(
                self.execution.run_tool,
                self.operation.id if self.operation else "",
                tool_id,
                {"command": command},
            )
        except ApprovalRequiredError as exc:
            self.kernel.approve(
                exc.approval_id,
                decided_by=("operator" if mode.prompts_for_tools and ask else mode.value),
            )
            approval_id = exc.approval_id
            try:
                result = await asyncio.to_thread(
                    self.execution.run_tool,
                    self.operation.id if self.operation else "",
                    tool_id,
                    {"command": command},
                    approval_id=approval_id,
                )
            except Exception as exc:  # noqa: BLE001 - fed back to the model
                self._write_line(f"[error] {exc}")
                return Message(role="user", content=f"[tool {call.name}] error: {exc}")
        except Exception as exc:  # noqa: BLE001 - fed back to the model
            self._write_line(f"[error] {exc}")
            return Message(role="user", content=f"[tool {call.name}] error: {exc}")
        exit_code = result.run.exit_code
        # Long tool output collapses under an arrow; /expand reveals it.
        self._expandable.append((command, exit_code, result.stdout, result.stderr))
        self._write_dim(
            self.translator.t(
                "chat.tool-collapsed",
                command=command,
                code=exit_code,
                lines=len(result.stdout.strip().splitlines()),
            )
        )
        stdout = result.stdout[: self._TOOL_OUTPUT_FEEDBACK_LIMIT]
        stderr = result.stderr[:1000]
        return Message(
            role="user",
            content=(
                f"[tool result {call.name} exit={exit_code}]\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ),
        )

    def _expand_outputs(self) -> None:
        """Reveal the collapsed tool output of the current reply in full."""
        if not self._expandable:
            self._write_dim(self.translator.t("chat.expand-empty"))
            return
        for command, exit_code, stdout, stderr in self._expandable:
            self._write_dim(
                f"▾ ⚙ {command} — " + self.translator.t("chat.tool-exit", code=exit_code)
            )
            text = stdout if stdout.strip() else stderr
            for line in text.rstrip().splitlines():
                self._write_dim(f"  {line}")

    def _copy_last(self, what: str) -> None:
        text = self._last_ai_code if what == "code" else self._last_ai_text
        if not text:
            key = "copy.code-empty" if what == "code" else "copy.empty"
            self._write_dim(self.translator.t(key))
            return
        self.copy_to_clipboard(text)
        self._write_dim(self.translator.t("copy.done", chars=len(text)))
