"""Chat side of the TUI: the agent turn loop, its gates and its controls.

Kept as a mixin so the loop can use the app's widgets and services directly
while ``app.py`` stays a shell: layout, bindings, status bar, command dispatch.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from rich.text import Text
from textual.widgets import Input, Static

from samurai_ai import __version__
from samurai_ai.agents.provider import ChatResponse, ImagePart, Message, ToolCall, ToolSpec
from samurai_ai.domain.execution import ApprovalRequiredError, SecretRedactor
from samurai_ai.domain.graph import (
    GraphError,
    GraphInvariantError,
    GraphNode,
    NodeKind,
    NodeState,
)
from samurai_ai.domain.kernel import (
    ArtifactKind,
    FindingSeverity,
    KernelError,
    PlanTask,
    RiskClass,
    TaskStatus,
)
from samurai_ai.domain.memory import MAX_MEMORY_TITLE_CHARS, MemoryKind
from samurai_ai.domain.models import ModelCapability, ProviderUnavailableError
from samurai_ai.domain.operations import Operation, OperationEvent, OperationMode
from samurai_ai.domain.permission import classify_command, decide
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization
from samurai_ai.domain.traffic import TrafficError
from samurai_ai.i18n import Translator
from samurai_ai.services.chat import ChatHistoryService, marker
from samurai_ai.services.graph import GraphService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.vault import VaultService
from samurai_ai.tui.journal import BlockJournal, ToolBlock, entity_span
from samurai_ai.tui.progress import Stagnation, TerminalReason
from samurai_ai.tui.screens import AccessPromptScreen

if TYPE_CHECKING:
    from samurai_ai.services.execution import ToolExecutionService
    from samurai_ai.services.goals import GoalService
    from samurai_ai.services.kernel import KernelService
    from samurai_ai.services.models import ModelService
    from samurai_ai.services.operations import OperationService
    from samurai_ai.services.settings import SettingsService
    from samurai_ai.storage.models import CustomModelConflictError  # noqa: F401


LEARN_BODY_LIMIT = 500
_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_IMAGE_TEXT_LIMIT = 4000
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_PROMISE_RE = re.compile(
    r"(?:проверю|проверим|сделаю|попробую|запущу|выполню|начну\b|далее\b|теперь\b"
    r"|i(?:'| wi)?ll\b|let me\b|going to\b|now i\b|next,? i\b)",
    re.IGNORECASE,
)
# A turn that ends on a promise of action is a broken turn: nudge the model to
# actually make the call instead of closing "completed". Bounded so a chatty
# model cannot spend the whole budget on promises.
_MAX_PROMISE_NUDGES = 2


def _resolve_image(raw: str, cwd: Path) -> Path:
    """Absolute-ise against the workspace, then resolve symlinks."""
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def _ocr(path: Path) -> str | None:
    """Text via tesseract; None when the host has no tesseract."""
    binary = shutil.which("tesseract")
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [binary, str(path), "stdout"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _one_line(text: str) -> str:
    """Collapse a lesson onto one line: memory digests are line-oriented."""
    return " ".join(text.split())


class ChatController:
    """Behaviour that needs the app's widgets; mixed into ``SamuraiApp``."""

    if TYPE_CHECKING:
        service: OperationService
        kernel: KernelService
        settings: SettingsService
        execution: ToolExecutionService
        goals: GoalService
        models: ModelService
        memory: CampaignMemoryService
        vault: VaultService
        translator: Translator
        operation: Operation | None
        _cwd: Any

        def paint(self, style: str) -> str: ...

        _chat_history: list[Message]
        _progress: Stagnation
        _last_turn_reason: TerminalReason | None
        chat_history: ChatHistoryService
        graph: GraphService
        lessons: Any
        constraints: Any
        traffic: Any
        _graph_used: bool
        _steer_queue: list[str]
        _chat_busy: bool
        _stop_requested: bool
        _stop_note_sent: bool
        screen: Any
        _shell_running: str
        _turn_started: float | None
        _current_tool: str
        _run_tokens: int
        _run_cost: float
        _shell_background: bool
        _ctx_tokens: int
        _process_block: list[str]
        _DIM_STYLE: str
        _last_ai_text: str | None
        _turn_todo: list[tuple[str, str]]
        _proposed_plan: tuple[str, list[str]] | None
        _last_user_text: str
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
        def _actor(self) -> str: ...
        def _write_dim(self, text: str, *, process: bool = True) -> None: ...
        def _close_process_block(self) -> None: ...
        def refresh_hunt(self) -> None: ...
        def _write_user_message(self, text: str) -> None: ...
        def _write_ai_message(self, response: Any) -> None: ...
        def _write_welcome(self) -> None: ...
        def _log_shadow_lessons(self) -> None: ...
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

    _CHAT_MAX_TOOL_ROUNDS = 32  # default; the setting tui.max_tool_rounds wins
    _TOOL_ROUNDS_MIN = 4
    _TOOL_ROUNDS_MAX = 200
    _CHAT_HISTORY_LIMIT = 40
    _TOOL_OUTPUT_FEEDBACK_LIMIT = 4000
    _MODEL_RETRY_ATTEMPTS = 5
    _MODEL_RETRY_DELAY = 3.0

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
                "Hand one plan task to a subagent session that works in "
                "the background. Prefer it for a self-contained chunk (a scan, "
                "a set of checks) while you keep going. One subagent runs at a "
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
        ToolSpec(
            name="open_intent",
            description=(
                "Open one exploration direction under a fact. Every intent hangs on "
                "the fact it grew from (omit parent_fact to use the newest confirmed "
                "fact or the task origin). One intent is one testable hypothesis."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "parent_fact": {"type": "string"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="record_fact",
            description=(
                "Record what an intent produced. Pass the artifact ids that prove it — "
                "a fact without an artifact is a claim. `result=negative` is for a "
                "checked-and-empty direction."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "minLength": 1},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "artifact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                    },
                    "result": {"type": "string", "enum": ["success", "negative"]},
                },
                "required": ["intent", "summary"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="close_intent",
            description=(
                "Close a direction: `done` needs a fact first, `blocked` and "
                "`exhausted` need a reason."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "minLength": 1},
                    "outcome": {"type": "string", "enum": ["done", "blocked", "exhausted"]},
                    "reason": {"type": "string", "maxLength": 500},
                },
                "required": ["intent", "outcome"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="record_traffic",
            description=(
                "Save one HTTP request/response pair as evidence for the operation. "
                "Bodies are clipped, not trusted to be small. Bind it to a finding "
                "afterwards with the board."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "minLength": 1, "maxLength": 16},
                    "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                    "status": {"type": "integer"},
                    "request_body": {"type": "string"},
                    "response_body": {"type": "string"},
                },
                "required": ["method", "url", "status"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="propose_finding",
            description=(
                "Register a finding that one recorded fact proves. The fact must carry "
                "an artifact; its artifacts become the finding's evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "severity": {
                        "type": "string",
                        "enum": ["info", "low", "medium", "high", "critical"],
                    },
                    "summary": {"type": "string", "maxLength": 2000},
                },
                "required": ["fact", "title"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="read_image",
            description=(
                "Read a screenshot or image inside the working directory: local OCR "
                "plus a vision model describe its text and content. Use it instead "
                "of guessing what a picture shows."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1, "maxLength": 4096}},
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="memory_learn",
            description=(
                "Write one lesson into the campaign memory of this workspace, so "
                "it outlives the session: a working method, a decision and its "
                "reason, or a failure worth not repeating. Keep it one fact."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["methodology", "decision", "failure"],
                    },
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "body": {"type": "string", "maxLength": 500},
                },
                "required": ["kind", "title"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="update_todo",
            description=(
                "Replace the turn's checklist. Send the whole list every time — it "
                "is displayed to the operator and replaces what was there. Keep it "
                "short (2-6 lines): what you are doing now, then what is next. "
                "Mark an item done as soon as it is done."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "done"],
                                },
                            },
                            "required": ["title", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="propose_plan",
            description=(
                "Propose the plan instead of running it. Allowed in plan mode. The "
                "operator sees the proposal and accepts or drops it; nothing runs "
                "until then."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 12,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    "notes": {"type": "string", "maxLength": 600},
                },
                "required": ["title", "tasks"],
                "additionalProperties": False,
            },
        ),
    )
    #: Chat tool name → registry tool id, for the ones that are real adapters.
    _CHAT_TOOL_IDS = {"shell_exec": "shell.exec"}

    _CHAT_TOOL_HANDLERS = {
        "shell_exec": "_run_shell_tool",
        "agent_spawn": "_run_spawn_tool",
        "memory_learn": "_run_learn_tool",
        "read_image": "_run_image_tool",
        "open_intent": "_run_open_intent_tool",
        "record_fact": "_run_record_fact_tool",
        "close_intent": "_run_close_intent_tool",
        "propose_finding": "_run_propose_finding_tool",
        "record_traffic": "_run_record_traffic_tool",
        "update_todo": "_run_update_todo_tool",
        "propose_plan": "_run_propose_plan_tool",
    }

    #: Braille spinner, the way quiet harnesses show work in progress.
    _THINKING_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    #: The colour walks the theme accent as the spinner turns, so the turn reads.
    _THINKING_RAMP = (
        "accent",
        "accent",
        "accent-alt",
        "accent-alt",
        "accent-alt",
        "accent",
    )

    def _chat_system_prompt(self) -> str:
        autonomy = self.translator.t(
            "chat.autonomy.never"
            if not self._permission_mode.allows_operator_questions
            else "chat.autonomy.ask"
        )
        prompt = self.translator.t(
            "chat.system",
            cwd=str(self._cwd),
            version=__version__,
            autonomy=autonomy,
        )
        prompt = self._append_constraints(prompt)
        prompt = self._append_frontier(prompt)
        try:
            digest, _ids = self.memory.digest(self._memory_workspace())
        except Exception:  # noqa: BLE001 - memory must not break a turn
            return prompt
        if not digest:
            return self._append_lessons(prompt)
        with_memory = f"{prompt}\n\n{self.translator.t('chat.memory', digest=digest)}"
        return self._append_lessons(with_memory)

    def _append_lessons(self, prompt: str) -> str:
        """Adopted lessons ride behind the memory digest; shadow mode adds nothing."""
        try:
            digest, _ids = self.lessons.digest(self._memory_workspace())
        except Exception:  # noqa: BLE001 - lessons must not break a turn
            return prompt
        if not digest:
            return prompt
        return f"{prompt}\n\n{self.translator.t('chat.lessons', digest=digest)}"

    def _append_constraints(self, prompt: str) -> str:
        """Operator boundaries go above everything: they outrank advice and history."""
        if self.operation is None:
            return prompt
        try:
            plan = self.kernel.get_current_plan(self.operation.id)
            if plan is None:
                return prompt
            block = "\n".join(
                text
                for text in (self.constraints.block(str(task.id)) for task in plan.tasks)
                if text
            )
        except Exception:  # noqa: BLE001 - constraints must not break a turn
            return prompt
        if not block:
            return prompt
        return f"{prompt}\n\n{self.translator.t('chat.constraints', block=block)}"

    def _append_frontier(self, prompt: str) -> str:
        """The open directions sit above the memory digest: a boundary beats advice."""
        if self.operation is None:
            return prompt
        try:
            plan = self.kernel.get_current_plan(self.operation.id)
            if plan is None:
                return prompt
            block = "\n".join(
                self.graph.frontier(str(task.id))
                for task in plan.tasks
                if self.graph.for_task(str(task.id))
            )
        except Exception:  # noqa: BLE001 - the graph must not break a turn
            return prompt
        if not block:
            return prompt
        return f"{prompt}\n\n{self.translator.t('chat.frontier', block=block)}"

    def _finding_count(self) -> int:
        if self.operation is None:
            return 0
        try:
            return len(self.kernel.kernel.list_findings(self.operation.id))
        except Exception:  # noqa: BLE001 - a count must never break a turn
            return 0

    def _note_stagnation(self) -> None:
        """Tell the model and the operator about a direction that stopped paying."""
        shape = self._progress.last_stuck
        if shape is None:
            return
        self._progress.last_stuck = None
        self._write_dim(self.translator.t("chat.stagnant", direction=shape))
        self._remember(
            Message(role="user", content=self.translator.t("chat.stagnant-note", direction=shape))
        )

    def _carry_previous_reason(self) -> None:
        """A failed previous turn tells the next one what to do differently."""
        last = self._last_turn_reason
        self._last_turn_reason = None
        if last is None or last is TerminalReason.COMPLETED:
            return
        self._remember(
            Message(
                role="user",
                content=self.translator.t(f"chat.prev-{last.value}"),
            )
        )


    def _write_retry_offer(self) -> None:
        """Offer to resend the failed turn without retyping it."""
        text = Text()
        text.append("[retry] ", style=self.paint("bold risk-mid"))
        text.append(self.translator.t("chat.retry-hint"), style=self.paint("text"))
        journal = self.query_one("#journal")
        journal.write(text)

    def action_retry_turn(self) -> None:
        """Ctrl+R or the hint's key: resend the last user message unchanged."""
        text = self._last_user_text
        if not text or getattr(self, "_chat_busy", False):
            return
        self._start_chat(text)

    def _end_turn(self, reason: TerminalReason) -> None:
        self._last_turn_reason = reason
        if reason is TerminalReason.MODEL_ERROR and self._last_user_text:
            # a dead gateway must not cost the operator their question
            self._write_retry_offer()
        if reason in (TerminalReason.ROUNDS, TerminalReason.MODEL_ERROR):
            # without a visible note the end of a turn reads as a freeze
            self._write_dim(self.translator.t(f"chat.ended-{reason.value}"), process=False)
        if self.operation is None:
            return
        self.service.repository.append_event(
            OperationEvent(
                operation_id=self.operation.id,
                event_type="chat.turn.end",
                payload={"reason": reason.value},
            )
        )

    def _restore_chat(self) -> None:
        """Bring the operation's earlier turns back into the context and the journal."""
        if self.operation is None:
            return
        messages, total, truncated = self.chat_history.tail(self.operation.id)
        if not messages:
            return
        system = Message(role="system", content=self._chat_system_prompt())
        self._chat_history = [system, *([marker()] if truncated else []), *messages]
        self._trim_chat_history()
        journal = self.query_one("#journal", BlockJournal)
        journal.write(Text())
        self._write_dim(self.translator.t("chat.restored", count=total))
        if truncated:
            self._write_dim(self.translator.t("chat.restored-cut"))
        journal.write(Text())
        for message in messages:
            text = str(message.content)
            if message.role == "assistant":
                journal.write(Text(text))
            elif text.startswith(("[tool ", "[shell ")):
                self._write_dim(text)
            else:
                self._write_user_message(text)
        self._write_dim(self.translator.t("chat.restored-end"))

    def _clear_chat(self) -> None:
        """Forget the operation's transcript in memory and in the database."""
        if self.operation is None:
            self._write_dim(self.translator.t("chat.clear-no-operation"))
            return
        if self._chat_busy:
            self._write_dim(self.translator.t("chat.clear-busy"))
            return
        removed = self.chat_history.clear(self.operation.id)
        self._chat_history.clear()
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._write_dim(self.translator.t("chat.cleared", count=removed))

    def _remember(self, message: Message) -> None:
        """Every message enters the context and the transcript together."""
        self._chat_history.append(message)
        if self.operation is not None:
            self.chat_history.remember(self.operation.id, message)

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
        """A turning glyph whose colour drifts: no words, no timer."""
        widget = self.query_one("#thinking", Static)
        frame = self._THINKING_FRAMES[self._thinking_frame % len(self._THINKING_FRAMES)]
        token = self._THINKING_RAMP[(self._thinking_frame // 3) % len(self._THINKING_RAMP)]
        widget.update(Text(frame, style=self.paint(f"bold {token}")))
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
        self._turn_todo: list[tuple[str, str]] = []  # a fresh checklist
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
        self._write_user_message(text)
        self._thinking_start()
        self.run_worker(self._chat(text, model_id, None), exclusive=False)

    def _shell_mode(self, command: str) -> None:
        self._current_tool = f"!{command}"
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
        self._remember(Message(role="user", content=f"[shell !{command}] (operator command)"))
        risk = classify_command(command, str(self._cwd))
        badge = f"  [{risk.label()}]" if not risk.empty else ""
        self._write_dim(f"⚙ !{command}{badge}")
        mode = self._permission_mode
        decision = decide(
            self.settings.permission_rules(),
            "shell.exec",
            command,
            mode,
            risk,
            reusable=self._reusable_approval("shell.exec"),
        )
        if decision.is_denied:
            self._write_dim(self.translator.t("chat.tool-denied", command=command), process=False)
            self._remember(Message(role="user", content=f"[shell !{command}] denied by rule"))
            self._shell_running = ""
            return
        ask = decision.action == "ask"
        if ask:
            allowed = await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    f"shell.exec: {command}",
                    f"r3 · {risk.label()}" if not risk.empty else "r3",
                )
            )
            if not allowed:
                self._write_dim(
                    self.translator.t("chat.tool-denied", command=command), process=False
                )
                self._remember(
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
                    decided_by=self._actor() if ask else mode.value,
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
            self._remember(Message(role="user", content=f"[shell !{command}] error: {exc}"))
            self._shell_running = ""
            return
        prefix = "[bg] " if self._shell_background else ""
        if not self._graph_used:
            self._progress.last_stuck = self._progress.record(command)
        self._write_tool_block(
            prefix
            + self.translator.t(
                "chat.tool-collapsed",
                command=f"!{command}",
                code=result.run.exit_code,
                lines=len(result.stdout.strip().splitlines()),
            ),
            result.run.exit_code,
            result.stdout,
            result.stderr,
            run_id=result.run.id,
        )
        body = (result.stdout if result.stdout.strip() else result.stderr).rstrip()
        self._remember(
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
        self._current_tool = ""

    async def _chat(self, text: str, model_id: str, approval_id: str | None) -> None:
        self._chat_busy = True
        self._stop_requested = False
        self._stop_note_sent = False
        self._turn_started = time.monotonic()
        self._current_tool = ""
        reason = TerminalReason.CANCELLED
        try:
            reason = await self._chat_loop(text, model_id, approval_id)
        finally:
            if self._stop_requested:
                # the window for the quit press counts from the stop, not from
                # the moment the operator asked for it
                self._last_quit_request = time.monotonic()
                self._write_dim(self.translator.t("app.stop-done"))
            self._stop_requested = False
            self._end_turn(reason)
            self._chat_busy = False
            self._turn_started = None
            self._current_tool = ""
            self._close_process_block()
            if reason is not TerminalReason.COMPLETED and self._collapsed:
                # a failure or a stop is exactly when the detail is wanted
                self._collapsed[-1].toggle()
            self._shell_running = ""
            self._shell_background = False
            self._thinking_stop()
            self._refresh_chrome()
            focus = getattr(self, "focus_command_input", None)
            if focus is not None:
                focus()
            else:  # pragma: no cover - the mixin may be used standalone
                self.query_one("#command-input", Input).focus()

    def request_stop(self) -> None:
        """Stop once the results are in and the hypotheses have been checked.

        Cutting at the next call boundary would leave tool results unread; the
        flag instead makes the loop finish the round, tell the model to conclude
        and end the turn at its conclusion.
        """
        self._stop_requested = True

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
                if self._stop_requested:
                    # the prompt is the step: stopping skips it rather than
                    # leaving the turn parked behind it
                    self.kernel.reject(exc.approval_id, decided_by=self._actor())
                    return None
                allowed = await self.push_screen_wait(
                    AccessPromptScreen(self.translator, action, risk)
                )
                if allowed:
                    self.kernel.approve(exc.approval_id, decided_by=self._actor())
                    self._write_dim(
                        "[allow] " + self.translator.t("access.granted", action=action),
                        process=False,
                    )
                    approval_id = exc.approval_id
                    continue
                self.kernel.reject(exc.approval_id, decided_by=self._actor())
                self._write_dim(
                    "[deny] " + self.translator.t("access.denied", action=action),
                    process=False,
                )
                return None

    async def _chat_loop(self, text: str, model_id: str, approval_id: str | None) -> TerminalReason:
        del approval_id  # approvals are handled per call inside the loop
        self._progress.reset()
        findings_before = self._finding_count()
        self._carry_previous_reason()
        self._log_shadow_lessons()
        reasoning = (
            self.settings.get_value(f"model.reasoning_effort.{model_id}")
            or self.models.default_reasoning_effort()
        )
        operation_id = self.operation.id if self.operation else ""
        if not self._chat_history:
            # rebuilt every launch, never persisted
            self._chat_history.append(Message(role="system", content=self._chat_system_prompt()))
        self._last_user_text = text
        rounds_left = self._max_tool_rounds()
        promise_nudges = 0
        self._remember(Message(role="user", content=text))
        for _round in range(rounds_left):
            self._drain_steering()
            self._count_goal_step()
            try:
                response = await self._request_chat_completion(
                    operation_id, model_id, reasoning, self._CHAT_TOOL_SPECS
                )
            except ProviderUnavailableError as exc:
                # a slow or sick gateway is the common case; the operator sees
                # the attempt counter instead of a turn that silently dies
                if exc.auth or self._stop_requested:
                    self._write_line(f"[error] {exc}")
                    return TerminalReason.MODEL_ERROR
                for attempt in range(1, self._MODEL_RETRY_ATTEMPTS + 1):
                    self._write_dim(
                        self.translator.t(
                            "chat.model-retry",
                            attempt=attempt,
                            total=self._MODEL_RETRY_ATTEMPTS,
                        )
                    )
                    await asyncio.sleep(self._MODEL_RETRY_DELAY)
                    try:
                        response = await self._request_chat_completion(
                            operation_id, model_id, reasoning, self._CHAT_TOOL_SPECS
                        )
                        break
                    except ProviderUnavailableError as retry_exc:
                        if retry_exc.auth or self._stop_requested:
                            self._write_line(f"[error] {retry_exc}")
                            return TerminalReason.MODEL_ERROR
                        if attempt == self._MODEL_RETRY_ATTEMPTS:
                            self._write_line(f"[error] {retry_exc}")
                            return TerminalReason.MODEL_ERROR
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                self._write_line(f"[error] {exc}")
                return TerminalReason.MODEL_ERROR
            if response is None:
                return TerminalReason.CANCELLED if self._stop_requested else TerminalReason.DENIED
            self._ctx_tokens += response.usage.prompt_tokens + response.usage.completion_tokens
            self._add_cost(model_id, response.usage.prompt_tokens, response.usage.completion_tokens)
            if not response.tool_calls:
                self._remember(Message(role="assistant", content=response.text))
                self._trim_chat_history()
                if response.text.strip():
                    self._write_ai_message(response)
                else:
                    self._write_dim(self.translator.t("chat.empty-reply"))
                if self._stop_requested:
                    # an answer without tools is the conclusion the stop waited for
                    return TerminalReason.CANCELLED
                if self._steer_queue:
                    # the operator steered while this answer was in flight
                    continue
                if promise_nudges < _MAX_PROMISE_NUDGES and _PROMISE_RE.search(response.text or ""):
                    promise_nudges += 1
                    self._write_dim(self.translator.t("chat.promise-nudge"))
                    self._remember(
                        Message(
                            role="user",
                            content=self.translator.t("chat.promise-note"),
                        )
                    )
                    continue
                return TerminalReason.COMPLETED
            if response.text.strip():
                self._remember(Message(role="assistant", content=response.text))
                # the reasoning between tool calls is the turn's narrative —
                # hiding it leaves the operator watching mute tool blocks
                self._write_ai_message(response)
            # every call of the round runs even when stopping: a stop is worth
            # nothing until the results are in and checked
            for call in response.tool_calls:
                self._remember(await self._run_chat_tool(call))
            self._trim_chat_history()
            if self._stop_requested and not self._stop_note_sent:
                self._stop_note_sent = True
                self._remember(Message(role="user", content=self.translator.t("chat.stop-note")))
            self._note_stagnation()
            if self._finding_count() > findings_before:
                findings_before = self._finding_count()
                self._progress.finding_seen()
        # The tool rounds are exhausted. Before anything else, a settlement pass:
        # the model may only record and close, so what it already found is written
        # down instead of dying with the turn. Then one closing answer without tools.
        self._write_dim(self.translator.t("chat.tool-limit"))
        await self._settle(operation_id, model_id, reasoning)
        try:
            final = await self._request_chat_completion(operation_id, model_id, reasoning, ())
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._write_line(f"[error] {exc}")
            return TerminalReason.MODEL_ERROR
        if final is None:
            return TerminalReason.DENIED
        self._ctx_tokens += final.usage.prompt_tokens + final.usage.completion_tokens
        self._add_cost(model_id, final.usage.prompt_tokens, final.usage.completion_tokens)
        self._remember(Message(role="assistant", content=final.text))
        self._trim_chat_history()
        if final.text.strip():
            self._write_ai_message(final)
        return TerminalReason.CANCELLED if self._stop_requested else TerminalReason.ROUNDS

    async def _settle(self, operation_id: str, model_id: str, reasoning: str | None) -> None:
        """One write-only round: persist what the turn found, without new exploration."""
        if not self._graph_used:
            return
        tools = tuple(
            spec
            for spec in self._CHAT_TOOL_SPECS
            if spec.name in ("record_fact", "close_intent", "propose_finding")
        )
        self._remember(Message(role="user", content=self.translator.t("chat.settle-prompt")))
        try:
            response = await self._request_chat_completion(operation_id, model_id, reasoning, tools)
        except Exception:  # noqa: BLE001 - the closing summary still has to happen
            return
        if response is None:
            return
        self._ctx_tokens += response.usage.prompt_tokens + response.usage.completion_tokens
        for call in response.tool_calls:
            self._remember(await self._run_chat_tool(call))
        if response.text.strip():
            self._remember(Message(role="assistant", content=response.text))

    def _add_cost(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Accumulate the run's spend; free models simply add nothing."""
        try:
            model, _provider = self.models.resolve_model(model_id)
        except Exception:  # noqa: BLE001 - an unpriced model is not an error
            return
        self._run_cost += model.estimate_cost(prompt_tokens, completion_tokens)

    def _max_tool_rounds(self) -> int:
        """Tool rounds per turn: the operator's setting, bounded, default 32.

        Eight rounds ended real HTB turns mid-work; the ceiling is a loop
        guard, not a budget — the loop detector stays the guard against waste.
        """
        raw = self.settings.get_value("tui.max_tool_rounds")
        try:
            value = int(raw) if raw else self._CHAT_MAX_TOOL_ROUNDS
        except ValueError:
            return self._CHAT_MAX_TOOL_ROUNDS
        return min(max(value, self._TOOL_ROUNDS_MIN), self._TOOL_ROUNDS_MAX)

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
            self._write_dim(self.translator.t("chat.tool-denied", command=call.name), process=False)
            return Message(
                role="user",
                content=f"[tool {call.name}] the operator has not trusted this directory",
            )
        try:
            result: Message = await getattr(self, handler)(call)
        except Exception as exc:  # noqa: BLE001 - one tool must not lose the turn
            # the model gets the failure as a result and can correct itself; an
            # escaping exception used to take the whole worker down instead
            self._write_line(f"[error] {call.name}: {exc}")
            return Message(role="user", content=f"[tool {call.name}] error: {exc}")
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
                content="[tool agent_spawn] the subagent did not start (see the journal)",
            )
        self._write_entity_line(
            self.translator.t("subagent.tool-started", run=run_id),
            "agent",
            run_id,
            run_id,
        )
        return Message(
            role="user",
            content=(
                f"[tool agent_spawn] run={run_id} task=<plan task> started in the "
                "background; its steps and output are in the sessions screen (Ctrl-G)"
            ),
        )

    # --- open exploration graph -------------------------------------------

    def _graph_task(self, title: str) -> str:
        """The task the graph hangs from: a free one, or a fresh single-task plan."""
        if self.operation is None:
            raise GraphInvariantError(self.translator.t("graph.no-operation"))
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is not None:
            for task in plan.tasks:
                if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
                    return str(task.id)
            raise GraphInvariantError(self.translator.t("graph.no-free-task"))
        task = PlanTask(id=f"chat-{uuid4().hex[:8]}", title=title[:200], risk_class=RiskClass.R2)
        self.kernel.create_plan(
            self.operation.id, self.translator.t("graph.plan-title"), [task], []
        )
        return task.id

    def _graph_root(self, task_id: str, title: str) -> GraphNode:
        assert self.operation is not None
        return self.graph.origin(self.operation.id, task_id, title)

    async def _run_open_intent_tool(self, call: ToolCall) -> Message:
        title = str(call.arguments.get("title", "") or "").strip()
        try:
            task_id = self._graph_task(title)
            root = self._graph_root(task_id, title)
            parent = str(call.arguments.get("parent_fact", "") or "").strip()
            if not parent:
                facts = [
                    node
                    for node in self.graph.for_task(task_id)
                    if node.kind is NodeKind.FACT and node.state is NodeState.CONFIRMED
                ]
                parent = facts[-1].id if facts else root.id
            assert self.operation is not None
            intent = self.graph.open_intent(self.operation.id, task_id, title, parent)
        except GraphError as exc:
            return Message(role="user", content=f"[tool open_intent] {exc}")
        self._graph_used = True
        self._progress.last_stuck = self._progress.record_shape(" ".join(title.lower().split()))
        self._write_dim(self.translator.t("graph.intent-opened", title=intent.text()))
        return Message(
            role="user",
            content=f"[tool open_intent] {intent.id} open: {intent.text()} (parent {parent})",
        )

    async def _run_record_fact_tool(self, call: ToolCall) -> Message:
        intent_id = str(call.arguments.get("intent", "") or "").strip()
        summary = str(call.arguments.get("summary", "") or "")
        raw = call.arguments.get("artifact_ids") or []
        artifacts = tuple(str(item) for item in raw if str(item).strip())
        try:
            intent = self._resolve_intent(intent_id)
            if intent.state is NodeState.OPEN:
                intent = self.graph.start_intent(intent.id)
            fact = self.graph.record_fact(
                intent.id,
                summary,
                artifacts,
                negative=str(call.arguments.get("result", "")) == "negative",
            )
            if fact.payload.get("artifact_ids"):
                pass
            else:
                self._write_dim(self.translator.t("graph.fact-bare"))
        except GraphError as exc:
            return Message(role="user", content=f"[tool record_fact] {exc}")
        if artifacts:
            self._progress.finding_seen()
        artifact_ids = cast("list[str]", fact.payload.get("artifact_ids") or [])
        if artifact_ids:
            self._write_entity_line(
                self.translator.t("graph.fact-recorded", fact=fact.id, intent=intent.id),
                "artifact",
                str(artifact_ids[0]),
                str(artifact_ids[0]),
            )
        else:
            self._write_dim(
                self.translator.t("graph.fact-recorded", fact=fact.id, intent=intent.id)
            )
        return Message(
            role="user", content=f"[tool record_fact] {fact.id} recorded for {intent.id}"
        )

    def _graph_tasks(self) -> list[str]:
        """Tasks of the current plan — the only place an intent can live."""
        if self.operation is None:
            return []
        plan = self.kernel.get_current_plan(self.operation.id)
        return [str(task.id) for task in plan.tasks] if plan is not None else []

    def _resolve_intent(self, intent_id: str) -> GraphNode:
        """The intent a model named, by id or by the title it typed instead.

        Models hand back titles as often as ids. A miss on both is worth
        creating: the direction was opened in the model's head either way.
        """
        try:
            return self.graph.get(intent_id)
        except GraphError:
            pass
        wanted = intent_id.strip().lower()
        for task in self._graph_tasks():
            for node in self.graph.for_task(task):
                if node.kind is NodeKind.INTENT and node.text().strip().lower() == wanted:
                    return node
        assert self.operation is not None
        task_id = self._graph_task(intent_id)
        parent = self._graph_root(task_id, intent_id)
        intent = self.graph.open_intent(self.operation.id, task_id, intent_id, parent.id)
        self._write_dim(self.translator.t("graph.intent-opened", title=intent.text()))
        return intent

    async def _run_close_intent_tool(self, call: ToolCall) -> Message:
        intent_id = str(call.arguments.get("intent", "") or "").strip()
        outcome = str(call.arguments.get("outcome", "") or "").strip()
        reason = str(call.arguments.get("reason", "") or "")
        try:
            intent = self.graph.close_intent(intent_id, outcome, reason)
        except GraphError as exc:
            return Message(role="user", content=f"[tool close_intent] {exc}")
        self._write_dim(self.translator.t("graph.intent-closed", intent=intent.id, state=outcome))
        return Message(role="user", content=f"[tool close_intent] {intent.id} is {outcome}")


    async def _run_propose_plan_tool(self, call: ToolCall) -> Message:
        """Show the proposal; the operator decides whether it becomes the plan."""
        title = str(call.arguments.get("title", "")).strip()
        raw_tasks = call.arguments.get("tasks")
        if not title or not isinstance(raw_tasks, list):
            return Message(
                role="user", content="[tool propose_plan] title and tasks are required"
            )
        tasks = [str(item).strip() for item in raw_tasks if str(item).strip()][:12]
        if not tasks:
            return Message(role="user", content="[tool propose_plan] no tasks given")
        self._proposed_plan = (title, tasks)
        block = Text()
        block.append("≡ " + title, style=self.paint("bold $accent"))
        for index, task in enumerate(tasks, start=1):
            block.append(f"\n  {index}. {task}", style=self.paint("$text"))
        notes = str(call.arguments.get("notes", "") or "").strip()
        if notes:
            block.append("\n" + notes, style=self.paint("dim"))
        block.append("\n" + self.translator.t("plan.proposal-hint"), style=self.paint("success"))
        self.query_one("#journal").write(block)
        refresh = getattr(self, "_refresh_ops_strip", None)
        if refresh is not None:
            refresh()
        return Message(
            role="user",
            content=f"[tool propose_plan] shown to the operator: {len(tasks)} tasks",
        )

    async def _run_update_todo_tool(self, call: ToolCall) -> Message:
        """The model owns this checklist; the operator only reads it."""
        items = call.arguments.get("items")
        if not isinstance(items, list):
            return Message(role="user", content="[tool update_todo] items must be a list")
        checklist: list[tuple[str, str]] = []
        for entry in items[:8]:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title", "")).strip()
            status = str(entry.get("status", "pending"))
            if title:
                checklist.append((title, status))
        self._turn_todo = checklist
        refresh = getattr(self, "_refresh_ops_strip", None)
        if refresh is not None:
            refresh()
        done = sum(1 for _, status in checklist if status == "done")
        return Message(role="user", content=f"[tool update_todo] {done}/{len(checklist)} done")

    async def _run_record_traffic_tool(self, call: ToolCall) -> Message:
        if self.operation is None:
            return Message(role="user", content="[tool record_traffic] no operation open")
        try:
            pair = self.traffic.record(
                self.operation.id,
                str(call.arguments.get("method", "") or ""),
                str(call.arguments.get("url", "") or ""),
                int(call.arguments.get("status", 0) or 0),
                str(call.arguments.get("request_body", "") or ""),
                str(call.arguments.get("response_body", "") or ""),
            )
        except (TrafficError, ValueError) as exc:
            return Message(role="user", content=f"[tool record_traffic] {exc}")
        clipped = self.translator.t("traffic.clipped") if pair.truncated else ""
        self._write_dim(self.translator.t("traffic.recorded", pair=pair.id, url=pair.url))
        return Message(
            role="user",
            content=(
                f"[tool record_traffic] {pair.id} {pair.method} {pair.url} -> "
                f"{pair.status}{clipped}"
            ),
        )

    async def _run_propose_finding_tool(self, call: ToolCall) -> Message:
        fact_id = str(call.arguments.get("fact", "") or "").strip()
        title = str(call.arguments.get("title", "") or "").strip()
        summary = str(call.arguments.get("summary", "") or "")
        raw_severity = str(call.arguments.get("severity", "") or "info")
        try:
            severity = FindingSeverity(raw_severity)
        except ValueError:
            severity = FindingSeverity.INFO
        try:
            fact = self.graph.get(fact_id)
            # Refuse before creating anything: a rejected proposal must not leave
            # a stray finding behind in the kernel.
            if fact.kind is not NodeKind.FACT:
                raise GraphInvariantError(f"{fact.id} is a {fact.kind.value}, not a fact")
            if not fact.payload.get("artifact_ids"):
                raise GraphInvariantError(f"fact {fact.id} has no artifact — evidence first")
            assert self.operation is not None
            finding = self.kernel.create_finding(self.operation.id, title, severity, summary)
            for artifact_id in cast("list[str]", fact.payload.get("artifact_ids") or []):
                self.kernel.attach_evidence(finding.id, str(artifact_id))
            self.graph.attach_finding(fact.id, finding.id)
        except (GraphError, KernelError) as exc:
            return Message(role="user", content=f"[tool propose_finding] {exc}")
        self._progress.finding_seen()
        self._write_entity_line(
            "◆ " + self.translator.t("graph.finding", finding=finding.id, title=finding.title),
            "finding",
            finding.id,
            self.translator.t("inspect.more"),
        )
        # a finding is the one thing worth showing the moment it lands
        self.refresh_hunt()
        return Message(
            role="user",
            content=(
                f"[tool propose_finding] {finding.id} registered on {fact.id} ({severity.value})"
            ),
        )

    async def _run_image_tool(self, call: ToolCall) -> Message:
        """OCR and a vision description of one image inside the trusted directory."""
        raw = str(call.arguments.get("path", "") or "").strip()
        if not raw:
            return Message(role="user", content="[tool error] read_image needs a path")
        if not self._directory_trusted():
            self._write_dim(
                self.translator.t("chat.tool-denied", command="read_image"), process=False
            )
            return Message(
                role="user", content="[tool read_image] the operator has not trusted this directory"
            )
        path = await asyncio.to_thread(_resolve_image, raw, self._cwd)
        if not path.is_relative_to(self._cwd):
            return Message(
                role="user",
                content=f"[tool read_image] outside the working directory: {path}",
            )
        if not path.is_file():
            return Message(role="user", content=f"[tool read_image] no such file: {path}")
        suffix = path.suffix.lower()
        mime = _IMAGE_TYPES.get(suffix)
        if mime is None:
            return Message(
                role="user",
                content=f"[tool read_image] unsupported type {suffix!r} "
                f"(allowed: {', '.join(sorted(_IMAGE_TYPES))})",
            )
        size = path.stat().st_size
        if size > _IMAGE_MAX_BYTES:
            return Message(
                role="user",
                content=f"[tool read_image] {size} bytes is over the {_IMAGE_MAX_BYTES} limit",
            )
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        self._write_dim(self.translator.t("image.reading", path=str(path), size=size))

        text = await asyncio.to_thread(_ocr, path)
        description, model_id = await self._describe_image(mime, data)
        if text is None and description is None:
            return Message(
                role="user",
                content="[tool read_image] no text extracted and no vision model available "
                "(install tesseract or pick a vision model with /model)",
            )
        parts = []
        if text:
            parts.append(f"text:\n{text}")
        elif text is not None:
            parts.append("text: (tesseract found no text)")
        if description:
            parts.append(f"description by {model_id}:\n{description}")
        else:
            parts.append("description: (no vision model — text only)")
        result = _bounded("\n\n".join(parts), _IMAGE_TEXT_LIMIT)
        if self.operation is not None:
            artifact = self.kernel.register_artifact(
                self.operation.id,
                ArtifactKind.OTHER,
                result,
                summary=f"image: {path.name}",
                created_by="agent",
            )
            self._write_dim(self.translator.t("image.stored", name=path.name, artifact=artifact.id))
        return Message(role="user", content=f"[tool read_image {path.name}]\n{result}")

    def _vision_model(self) -> str | None:
        """The chat model when it can see, else the configured vision model."""
        default = self.models.default_model()
        if default:
            try:
                model, _provider = self.models.registry.get_model(default)
            except Exception:  # noqa: BLE001 - unknown id falls through to the setting
                model = None
            if model is not None and ModelCapability.VISION in model.capabilities:
                return default
        configured = (self.settings.get_value("model.vision") or "").strip()
        if configured:
            return configured
        return None

    async def _describe_image(self, mime: str, data: str) -> tuple[str | None, str | None]:
        """One vision call; None when there is no model or the call fails."""
        model_id = self._vision_model()
        if model_id is None or self.operation is None:
            return None, None
        prompt = Message(role="user", content=self.translator.t("image.prompt"))
        try:
            response = await asyncio.to_thread(
                self.models.chat,
                self.operation.id,
                model_id,
                (prompt,),
                images=(ImagePart(mime=mime, data=data),),
            )
        except ApprovalRequiredError as exc:
            if self._yolo:
                self.kernel.approve(exc.approval_id, decided_by="auto")
                allowed = True
            else:
                allowed = await self.push_screen_wait(
                    AccessPromptScreen(self.translator, f"model.call {model_id}", "r2")
                )
            if not allowed:
                self.kernel.reject(exc.approval_id, decided_by=self._actor())
                return None, None
            if not self._yolo:  # yolo already approved it above
                self.kernel.approve(exc.approval_id, decided_by=self._actor())
            try:
                response = await asyncio.to_thread(
                    self.models.chat,
                    self.operation.id,
                    model_id,
                    (prompt,),
                    images=(ImagePart(mime=mime, data=data),),
                    approval_id=exc.approval_id,
                )
            except Exception as exc2:  # noqa: BLE001 - fed back to the model
                return f"(vision call failed: {exc2})", model_id
        except Exception as exc:  # noqa: BLE001 - fed back to the model
            return f"(vision call failed: {exc})", model_id
        return response.text.strip(), model_id

    async def _run_learn_tool(self, call: ToolCall) -> Message:
        """A lesson the model wrote down: closed shape, redacted, audited."""
        if self.settings.get_value("memory.agent_writes") == "0":
            return Message(
                role="user",
                content="[tool memory_learn] agent writes are off (setting memory.agent_writes)",
            )
        raw_kind = str(call.arguments.get("kind", "") or "")
        title = _one_line(str(call.arguments.get("title", "") or ""))
        body = _one_line(str(call.arguments.get("body", "") or ""))
        if not title:
            return Message(role="user", content="[tool error] memory_learn needs a title")
        try:
            kind = MemoryKind(raw_kind)
        except ValueError:
            kind = MemoryKind.NOTE
        if kind is MemoryKind.NOTE:  # notes belong to the operator
            kind = MemoryKind.METHODOLOGY
        redactor = SecretRedactor()
        for metadata in self.vault.list_metadata():
            redactor.register(self.vault.get(metadata.name))
        try:
            entry = self.memory.add(
                self._memory_workspace(),
                redactor.redact(title).text[:MAX_MEMORY_TITLE_CHARS],
                redactor.redact(body).text[:LEARN_BODY_LIMIT],
                kind=kind,
                created_by="agent",
            )
        except Exception as exc:  # noqa: BLE001 - fed back to the model
            return Message(role="user", content=f"[tool memory_learn] error: {exc}")
        if self.operation is not None:
            self.service.repository.append_event(
                OperationEvent(
                    operation_id=self.operation.id,
                    event_type="memory.learned",
                    payload={
                        "entry_id": entry.id,
                        "kind": kind.value,
                        "workspace": entry.workspace,
                    },
                )
            )
        self._write_dim(self.translator.t("memory.learned", kind=kind.value, title=entry.title))
        return Message(
            role="user",
            content=f"[tool memory_learn] stored {entry.id} ({kind.value}) in {entry.workspace}",
        )

    def _memory_workspace(self) -> str:
        """The directory name, slugged to the kebab-case memory workspaces require."""
        slug = re.sub(r"[^a-z0-9]+", "-", self._cwd.name.lower()).strip("-")
        return slug or "workspace"

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
        self._current_tool = command
        # A tool already approved for this operation is not asked again, except
        # for a sensitive path or a destructive shape, which stay worth a look.
        mode = self._permission_mode
        decision = decide(
            self.settings.permission_rules(),
            tool_id,
            command,
            mode,
            risk,
            reusable=self._reusable_approval(tool_id),
        )
        if decision.is_denied:
            self._write_dim(self.translator.t("chat.tool-denied", command=command), process=False)
            return Message(role="user", content=f"[tool {call.name}] denied by rule")
        ask = decision.action == "ask"
        if ask:
            allowed = await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    f"{tool_id}: {command}",
                    f"r3{badge}" if risk.empty else f"r3 · {risk.label()}",
                )
            )
            if not allowed:
                self._write_dim(
                    self.translator.t("chat.tool-denied", command=command), process=False
                )
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
        self._progress.last_stuck = None if self._graph_used else self._progress.record(command)
        # The run lands as a collapsible block; a failure opens on its own.
        self._write_tool_block(
            self.translator.t(
                "chat.tool-collapsed",
                command=command,
                code=exit_code,
                lines=len(result.stdout.strip().splitlines()),
            ),
            exit_code,
            result.stdout,
            result.stderr,
            run_id=result.run.id,
        )
        stdout = result.stdout[: self._TOOL_OUTPUT_FEEDBACK_LIMIT]
        stderr = result.stderr[:1000]
        return Message(
            role="user",
            content=(
                f"[tool result {call.name} exit={exit_code}]\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ),
        )

    def _write_tool_block(
        self,
        title: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        run_id: str | None = None,
    ) -> None:
        """A tool run as a collapsible block; a failure speaks and opens.

        A run that failed with nothing on stderr used to leave a bare line in
        the journal, which reads like a hang — so the header of a failed block
        carries the last error line, or says outright that there was no output.
        """
        journal = self.query_one("#journal", BlockJournal)
        failed = exit_code not in (0, None)
        if failed:
            error = (stderr.strip() or stdout.strip()).splitlines()
            if error:
                title += f"  ← {error[-1][:120]}"
            else:
                title += f"  ← exit {exit_code}, no output"
        body = (stdout if stdout.strip() else stderr).rstrip()
        title_text = Text(title, style=self.paint(self._DIM_STYLE))
        if run_id:
            title_text.append(" ")
            title_text.append_text(entity_span("ⓘ", "run", run_id, base_style=self.paint("accent")))
        journal.add_tool_block(
            title_text,
            body.splitlines() if body else [],
            failed=failed,
            body_style=self.paint(self._DIM_STYLE),
        )
        self._current_tool = ""

    def _write_entity_line(self, prefix: str, kind: str, ref: str, label: str) -> None:
        """A journal line whose tail is a clickable entity link."""
        journal = self.query_one("#journal", BlockJournal)
        journal.write(
            Text.assemble(
                (prefix + " ", self.paint(self._DIM_STYLE)),
                entity_span(label, kind, ref, base_style=self.paint("accent")),
            )
        )

    @property
    def _collapsed(self) -> list[ToolBlock]:
        """Folded journal blocks, oldest first (was a list of tuples)."""
        journal = self.query_one("#journal", BlockJournal)
        return [block for block in journal.tool_blocks if not block.expanded]

    def _expand_outputs(self) -> None:
        """Reveal the oldest folded block, so presses walk down the reply."""
        journal = self.query_one("#journal", BlockJournal)
        if journal.expand_oldest() is None:
            self._write_dim(self.translator.t("chat.expand-empty"))

    def _copy_last(self, what: str = "text") -> None:
        """`/copy` takes the last answer; `/copy code` only its code block."""
        text = self._last_ai_code if what.strip() == "code" else self._last_ai_text
        if not text:
            key = "copy.code-empty" if what == "code" else "copy.empty"
            self._write_dim(self.translator.t(key))
            return
        self.copy_to_clipboard(text)
        self._write_dim(self.translator.t("copy.done", chars=len(text)))
