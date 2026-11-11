"""Chat side of the TUI: the agent turn loop, its gates and its controls.

Kept as a mixin so the loop can use the app's widgets and services directly
while ``app.py`` stays a shell: layout, bindings, status bar, command dispatch.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from rich.text import Text
from textual.widgets import Input, Static

from samurai_ai import __version__
from samurai_ai.agents.provider import (
    STREAM_RESET,
    ChatResponse,
    ImagePart,
    Message,
    ToolCall,
    ToolSpec,
)
from samurai_ai.domain.execution import (
    ApprovalRequiredError,
    SecretRedactor,
    ToolScopeError,
)
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
from samurai_ai.domain.permission import (
    CommandRisk,
    bound_network_command,
    classify_command,
    command_window,
    decide,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.swarm import AgentSpecialization
from samurai_ai.domain.traffic import TrafficError
from samurai_ai.i18n import Translator
from samurai_ai.services import goal_judge
from samurai_ai.services.chat import ChatHistoryService, marker
from samurai_ai.services.graph import GraphService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.vault import VaultService
from samurai_ai.tui.journal import BlockJournal, ToolBlock, entity_span
from samurai_ai.tui.progress import Stagnation, TerminalReason
from samurai_ai.tui.screens import AccessPromptScreen, PickerScreen

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
    # present-tense announcements die as often as future promises:
    # «Читаю остальное: …» ended an HTB turn mid-work for 80 minutes
    r"|читаю\b|смотрю\b|разбираю\b|собираю\b|изучаю\b|качаю\b|забираю\b|перечисляю\b"
    r"|i(?:'| wi)?ll\b|let me\b|going to\b|now i\b|next,? i\b"
    r"|reading\b|checking\b|looking\b|fetching\b|downloading\b|examining\b|reviewing\b)",
    re.IGNORECASE,
)
# A turn that ends on a promise of action is a broken turn: nudge the model to
# actually make the call instead of closing "completed". Bounded so a chatty
# model cannot spend the whole budget on promises.
_MAX_PROMISE_NUDGES = 2
#: a worked turn (tool calls happened) that claims to be done while a goal
#: exists gets judged before it may end; this many times per turn at most
_MAX_COMPLETION_JUDGES = 2


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


def _bounded_call(call: ToolCall) -> ToolCall:
    """The command the approval and the process will both actually run."""
    if call.name != "shell_exec":
        return call
    command = str(call.arguments.get("command", ""))
    bounded = bound_network_command(command)
    if bounded == command:
        return call
    return call.model_copy(update={"arguments": {**call.arguments, "command": bounded}})


def _short_tool(tool_id: str, command: str) -> str:
    """`bash · prlctl exec … echo…` — the tool plus a cut command, one line."""
    name = tool_id.split(".")[-1]
    head = command.split()[0] if command.split() else ""
    return f"{name} · {head} …" if head and len(command.split()) > 1 else name


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
        _run_costs: dict[str, float]
        _run_cost_unknown: set[str]
        _shell_background: bool
        _ctx_tokens: int
        _sent_tokens: int
        _recv_tokens: int
        _running_tools: int
        _cache_share: float
        _round_approvals: dict[str, bool]
        _background_results: list[Message]
        _process_block: list[str]
        _DIM_STYLE: str
        _last_ai_text: str | None
        _turn_todo: list[tuple[str, str]]
        _proposed_plan: tuple[str, list[dict[str, object]]] | None
        _last_user_text: str
        _live_answer: Any
        def _refresh_side_blocks(self) -> None: ...
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
        def call_from_thread(self, callback: Any, *args: Any) -> Any: ...
        size: Any
        def set_interval(self, *args: Any, **kwargs: Any) -> Any: ...
        def _directory_trusted(self) -> bool: ...
        def _tool_is_read_only(self, tool_id: str) -> bool: ...
        def spawn_subagent(self, instruction: str, **kwargs: Any) -> Any: ...
        def _reusable_approval(self, tool_id: str) -> bool: ...
        def _drain_steering(self) -> None: ...
        def _count_goal_step(self) -> None: ...
        def run_worker(self, *args: Any, **kwargs: Any) -> Any: ...
        def push_screen_wait(self, screen: Any) -> Any: ...
        _relay_offered: set[str]
        _scope_offered: set[str]
        _fresh_task_text: str | None
        _unstructured_shells: int
        _structure_nudged: bool
        _drift_nudged: bool
        _drift_marks: Any
        _strategist_spawned_turn: bool
        def _relay_canary(self, relay: str) -> None: ...

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

    _CHAT_MAX_TOOL_ROUNDS = 200  # default; the setting tui.max_tool_rounds wins
    _AUTO_CONTINUES = 5
    #: bare shell calls before the turn is told to write its state down
    _STRUCTURE_NUDGE_AT = 8
    #: out-of-workspace reads inside the last 8 commands that mean drift
    _DRIFT_NUDGE_AT = 3
    #: scratch and toolchain roots are not drift; everything else outside
    #: the workspace during an operation is
    _DRIFT_ALLOWED = ("/tmp", "/var", "/usr", "/bin", "/sbin", "/opt", "/System", "/Library")
    _TOOL_ROUNDS_MIN = 4
    _TOOL_ROUNDS_MAX = 600
    _CHAT_HISTORY_LIMIT = 40
    _TOOL_OUTPUT_FEEDBACK_LIMIT = 4000
    _MODEL_RETRY_ATTEMPTS = 5
    _MODEL_RETRY_DELAY = 3.0
    #: how long a command may hold the turn before it becomes a background run
    _FOREGROUND_WINDOW = 25.0
    #: how many calls of one answer run at the same time
    _PARALLEL_LIMIT = 8

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
                        "enum": [
                            "general", "recon", "testing",
                            "analysis", "strategist", "researcher",
                        ],
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "Plan task id this subagent works on; "
                            "omit to guess from the instruction"
                        ),
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
                    "next_test": {
                        "type": "string",
                        "maxLength": 200,
                        "description": "The next test that could falsify this, in one line.",
                    },
                    "stop_condition": {
                        "type": "string",
                        "maxLength": 160,
                        "description": "When to give up on it, before starting the work.",
                    },
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
            name="provision_tool",
            description=(
                "Install a tool that is missing on this machine, from the reviewed "
                "catalogue, then run it once more. Use it only when a run answered "
                "that the binary is missing: a scope refusal, a bad parameter or a "
                "timeout do not need an install. The plan is shown and approved "
                "before anything changes."
            ),
            parameters={
                "type": "object",
                "properties": {"tool_id": {"type": "string", "maxLength": 64}},
                "required": ["tool_id"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="set_relay",
            description=(
                "Point this operation's network commands through a relay prefix "
                "(e.g. prlctl exec '<vm-uuid>' or ssh -J jump) when the target is "
                "only reachable from another host or VM. After it is set, write "
                "plain far-side commands — the harness rewrites and quotes them "
                "for the relay itself. An empty prefix clears the relay."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "maxLength": 400},
                },
                "required": ["prefix"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="check_scope",
            description=(
                "Check whether a target (IP, domain, URL or network) is inside "
                "this operation's scope before touching it. Answers allowed, "
                "excluded or out-of-scope with the matching rule. Ask before "
                "working a host the SCOPE board does not show."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "minLength": 1, "maxLength": 300},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="add_scope",
            description=(
                "Widen this operation's scope with targets discovered mid-run "
                "(subdomains, pivot hosts, an alias a vhost answers to). Each "
                "addition is journaled and the operator can revert it with "
                "/scope rm. Targets already covered are skipped silently. "
                "Prefer one wildcard rule (*.example.com) over many subdomains."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 50,
                    },
                },
                "required": ["targets"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="remember_secret",
            description=(
                "Store a credential found during the work (password, token, key, "
                "hash) in the operation vault so later calls can use it and the "
                "side panel shows it. Name it lower-case, e.g. kpark-ssh-password."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "value": {"type": "string", "minLength": 1, "maxLength": 8192},
                },
                "required": ["name", "value"],
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
                        "items": {
                            "oneOf": [
                                {"type": "string", "minLength": 1, "maxLength": 200},
                                {
                                    "type": "object",
                                    "properties": {
                                        "title": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 200,
                                        },
                                        "depends_on": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "risk_class": {
                                            "type": "string",
                                            "enum": ["R0", "R1", "R2", "R3", "R4"],
                                        },
                                    },
                                    "required": ["title"],
                                    "additionalProperties": False,
                                },
                            ]
                        },
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

    #: Relay shapes a task text can mention; the first with a concrete target wins.
    _RELAY_CANDIDATE_RES = (
        re.compile(r"\bprlctl\s+exec\s+('[^']+'|\"[^\"]+\"|[\w.{}-]+)"),
        re.compile(r"\bssh\s+-J\s+\S+"),
        re.compile(r"\butmctl\s+exec\s+\S+"),
        re.compile(r"\blimactl\s+shell\s+\S+"),
    )
    #: the same tools named without a target — the operator must fill it in
    _BARE_RELAY_RE = re.compile(r"\b(?:prlctl\s+exec|utmctl\s+exec|limactl\s+shell)\b")

    #: networks a task text can name; the bare IP part is already in _HOST_RE
    _CIDR_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}\b")

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
        "remember_secret": "_run_remember_secret_tool",
        "set_relay": "_run_set_relay_tool",
        "provision_tool": "_run_provision_tool",
        "check_scope": "_run_check_scope_tool",
        "request_scope": "_run_request_scope_tool",
        "add_scope": "_run_request_scope_tool",
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

    def _recall_for(self, text: str) -> str:
        """Past memory entries that match the user's text; empty when none."""
        try:
            entries = self.memory.recall(text)
        except Exception:  # noqa: BLE001 - memory must not break a turn
            return ""
        if not entries:
            return ""
        lines = ["Related campaign memory:"]
        for entry in entries[:4]:
            lines.append(f"- {entry.title}: {entry.body[:120]}")
        return "\n".join(lines)

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
        if self.operation is not None:
            # the operator doctrine: generic «you have a shell» prompts produced
            # crawler behaviour (HTB Takeoff never registered an account)
            prompt += "\n\n" + self.translator.t("chat.doctrine")
        goal = self._operation_goal(self.operation.id) if self.operation else ""
        if goal:
            prompt += "\n\n" + self.translator.t("chat.goal-hint", goal=goal)
        relay = getattr(self.operation, "relay", "") if self.operation else ""
        if relay:
            # hand the relay to the model as-is so it never invents a helper
            prompt += "\n\n" + self.translator.t("chat.relay-hint", relay=relay)
        elif self._remote_scope():
            prompt += "\n\n" + self.translator.t("chat.relay-absent")
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

    #: tools whose job starts after the foothold: their long prose is wasted on
    #: recon, and the whole block rides in every request
    _LATER_TOOLS = frozenset(
        {"propose_finding", "record_traffic", "remember_secret", "propose_plan", "read_image"}
    )
    _SHORT_DESCRIPTIONS = {
        "propose_finding": "Record a finding for the operation (title, summary, severity).",
        "record_traffic": "Attach a request/response pair to a finding as evidence.",
        "remember_secret": "Store a secret in the operation vault.",
        "propose_plan": "Propose a plan of tasks for the operation.",
        "read_image": "Read an image file and describe it.",
    }

    def _tool_specs(self) -> tuple[ToolSpec, ...]:
        """The tool block for this turn: same schemas, prose only where it helps.

        Every tool stays callable — dropping one is how the model ends up unable
        to record the finding it just made — but a tool that belongs to a later
        stage does not need its full description on a recon turn.
        """
        early = getattr(self, "_stage_label", "") in ("", "recon", "surface")
        if not early:
            return self._CHAT_TOOL_SPECS
        return tuple(
            spec.model_copy(
                update={"description": self._SHORT_DESCRIPTIONS[spec.name]}
            )
            if spec.name in self._LATER_TOOLS
            else spec
            for spec in self._CHAT_TOOL_SPECS
        )

    def _approval_meta(
        self,
        tool_id: str,
        command: str,
        risk: CommandRisk,
        *,
        batch: list[tuple[str, str]] | None = None,
    ) -> Any:
        """The consequences of a command, for the approval screen to state.

        Only facts the harness already holds: a wrong or invented value in a
        prompt is worse than a missing line.
        """
        from samurai_ai.tui.screens.access import ApprovalMeta

        relay = self.operation.relay if self.operation is not None else ""
        timeout = command_window(command, relayed=bool(relay))
        return ApprovalMeta(
            tool_id=tool_id,
            cwd=str(self._cwd),
            execution_path=relay or self.translator.t("access.path-native"),
            timeout_s=timeout,
            destructive=risk.destructive,
            sensitive=risk.sensitive,
            batch=tuple(label for label, _risk in (batch or [])),
        )

    def _count_usage(self, usage: Any) -> None:
        """One place that sees every model answer: context gauge and totals.

        A prompt already carries the whole conversation sent with it, so the
        window fill is a *reading* of the last call, not a running sum — summing
        it reported the same history once per call and showed 714k on a session
        whose real window peaked at 10k. The ⇈/⇊/Σ totals stay cumulative on
        purpose: they are spend, not occupancy.
        """
        self._ctx_tokens = usage.prompt_tokens + usage.completion_tokens
        self._sent_tokens += usage.prompt_tokens
        self._recv_tokens += usage.completion_tokens
        # a provider-side prefix cache is the cheapest speed there is; it stays
        # invisible unless the share is said out loud, so it goes in the journal
        hit = float(getattr(usage, "cache_hit", 0.0) or 0.0)
        if hit:
            self._cache_share = hit
            self._write_dim(self.translator.t("chat.cache-hit", share=f"{hit:.0%}"), process=False)

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

    async def _maybe_spawn_strategist(self, fact_summary: str) -> None:
        """fact.confirmed → the strategist re-reads the map and proposes vectors."""
        if self.operation is None or self._strategist_spawned_turn:
            return
        self._strategist_spawned_turn = True
        instruction = self.translator.t("strategist.review-task", fact=fact_summary[:200])
        run_id = await self.spawn_subagent(
            instruction, specialization=AgentSpecialization.STRATEGIST
        )
        if run_id is not None:
            self._write_dim(self.translator.t("strategist.spawned", run=run_id))

    def _schedule_strategist_review(self) -> None:
        """Between turns: one strategist run for everything the turn proved."""
        if not self._turn_facts or self._strategist_spawned_turn:
            return
        facts = "; ".join(self._turn_facts[-5:])
        self.run_worker(self._maybe_spawn_strategist(facts), exclusive=False)

    def _note_unstructured_shell(self) -> None:
        """Shell calls with nothing recorded are a crawler, not an operation.

        HTB Takeoff burned a whole turn that way: the state died with the
        context because no fact, intent or finding was ever written.
        """
        if self.operation is None or self._structure_nudged:
            return
        self._unstructured_shells += 1
        if self._unstructured_shells < self._STRUCTURE_NUDGE_AT:
            return
        self._structure_nudged = True
        self._write_dim(
            self.translator.t("chat.structure-nudge", count=self._unstructured_shells)
        )
        self._steer_queue.append(self.translator.t("chat.structure-note"))

    def _note_structure_written(self) -> None:
        """A fact, intent or finding re-arms the bare-shell counter."""
        self._unstructured_shells = 0

    def _note_local_drift(self, command: str) -> None:
        """Repeated reads of local paths outside the workspace are drift."""
        if self.operation is None or self._drift_nudged:
            return
        if self.operation.relay:
            # with a relay set the command runs on the far side: its paths
            # belong to the target, not to this host
            return
        self._drift_marks.append(bool(self._drift_paths(command)))
        if sum(self._drift_marks) < self._DRIFT_NUDGE_AT:
            return
        self._drift_nudged = True
        self._write_dim(self.translator.t("chat.drift-nudge"))
        self._steer_queue.append(self.translator.t("chat.drift-note"))

    def _drift_paths(self, command: str) -> list[str]:
        """Absolute local paths a command reads outside the workspace."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        home = str(Path.home())
        cwd = str(self._cwd)
        hits: list[str] = []
        for token in tokens:
            if "://" in token or token.startswith("-"):
                continue
            bare = token.strip("'\"")
            if bare.startswith("~/"):
                bare = home + bare[1:]
            if not bare.startswith("/"):
                continue
            if bare.startswith(cwd) or bare.startswith(self._DRIFT_ALLOWED):
                continue
            if bare.startswith(home + "/.config/samurai"):
                continue
            hits.append(bare)
        return hits

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


    def _stream_token(self, token: str) -> None:
        """One token of the model's answer into the live block, thread-safe."""
        block = self._live_answer
        if block is None:
            return
        if token == STREAM_RESET:
            # a fallback is about to resend the whole answer: drop the partial
            if getattr(self, "_thread_id", None) == threading.get_ident():
                block.reset_tokens()
            else:
                self.call_from_thread(block.reset_tokens)
            return
        # on a worker thread (real run) we hop to the app thread; on the test
        # harness the pilot runs inline and a hop would deadlock
        if getattr(self, "_thread_id", None) == threading.get_ident():
            block.append_token(token)
        else:
            self.call_from_thread(block.append_token, token)

    def _begin_live_answer(self) -> None:
        """Open the streaming block; the final message replaces it."""
        from samurai_ai.tui.journal import LiveAnswerBlock

        journal = self.query_one("#journal", BlockJournal)
        block = LiveAnswerBlock()
        if getattr(self, "_thread_id", None) == threading.get_ident():
            journal.add_block(block)
        else:
            self.call_from_thread(journal.add_block, block)
        self._live_answer = block

    def _end_live_answer(self) -> None:
        self._live_answer = None

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
        self._write_dim(self.translator.t("chat.restored", count=total), process=False)
        if truncated:
            self._write_dim(self.translator.t("chat.restored-cut"), process=False)
        journal.write(Text())
        for message in messages:
            text = str(message.content)
            if message.role == "assistant":
                journal.write(Text(text))
            elif text.startswith(("[tool ", "[shell ")):
                self._write_dim(text)
            else:
                self._write_user_message(text)
        self._write_dim(self.translator.t("chat.restored-end"), process=False)

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
        self._ctx_tokens = 0
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
            # the targets in the task text are the operator's own words — they
            # become the scope without a confirmation picker
            self._fresh_task_text = text
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
        # The operator can retry a timed-out command deliberately; its transport
        # bounds are still the same ones shown in the approval.
        command = bound_network_command(command)
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
                    command,
                    risk.label() or "r3",
                    meta=self._approval_meta("shell.exec", command, risk),
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
        except ToolScopeError as exc:
            # the guard working as designed is information, not an error
            journal = self.query_one("#journal", BlockJournal)
            journal.write(Text(f"[scope] {exc}", style=self.paint("accent")))
            self._remember(
                Message(role="user", content=f"[shell !{command}] out of scope: {exc}")
            )
            self._shell_running = ""
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._write_line(f"[error] {exc}")
            self._remember(Message(role="user", content=f"[shell !{command}] error: {exc}"))
            self._shell_running = ""
            return
        prefix = "[bg] " if self._shell_background else ""
        if not self._graph_used:
            self._progress.last_stuck = self._progress.record(command)
            self._note_unstructured_shell()
        self._note_local_drift(command)
        self._write_tool_block(
            prefix
            + self.translator.t(
                "chat.tool-collapsed",
                command=f"!{command}",
                code=result.run.exit_code,
                code_mark="" if result.run.exit_code == 0 else f" ✗{result.run.exit_code}",
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
            if reason is not TerminalReason.COMPLETED:
                # a failure or a stop is exactly when the detail is wanted
                block = getattr(self, "_thinking_block", None)
                if block is not None:
                    block.toggle()
                elif self._collapsed:
                    self._collapsed[-1].toggle()
            self._close_process_block()
            self._shell_running = ""
            self._shell_background = False
            self._thinking_stop()
            self._schedule_strategist_review()
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
        max_tokens: int | None = None,
        messages: tuple[Message, ...] | None = None,
    ) -> ChatResponse | None:
        """One model call with inline approval handling; None = denied/failed."""
        approval_id: str | None = None
        while True:
            try:
                return await asyncio.to_thread(
                    self.models.chat,
                    operation_id,
                    model_id,
                    messages if messages is not None else tuple(self._chat_history),
                    tools=tools,
                    reasoning_effort=reasoning,
                    approval_id=approval_id,
                    max_tokens=max_tokens,
                    on_token=self._stream_token,
                    should_stop=lambda: self._stop_requested,
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
        self._unstructured_shells = 0
        self._structure_nudged = False
        self._drift_marks.clear()
        self._drift_nudged = False
        self._strategist_spawned_turn = False
        self._turn_facts: list[str] = []
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
        self._adopt_goal(text)
        await self._maybe_offer_relay(text)
        await self._maybe_offer_scope(text)
        recall = self._recall_for(text)
        if recall:
            self._remember(Message(role="system", content=recall))
        rounds_left = self._max_tool_rounds()
        stretches_used = 0
        promise_nudges = 0
        completion_judges = 0
        tool_calls_made = 0
        self._remember(Message(role="user", content=text))
        self._begin_live_answer()
        while True:
            if rounds_left <= 0:
                # The budget for this stretch is spent. Kimi/OpenCode keep a step
                # cap as a safety net, not the normal exit: a run ends when the
                # model stops calling tools. So settle what is known, then refill.
                self._write_dim(self.translator.t("chat.tool-limit"))
                await self._settle(operation_id, model_id, reasoning)
                if self._stop_requested:
                    return TerminalReason.CANCELLED
                if stretches_used >= self._AUTO_CONTINUES:
                    break
                if self._operation_goal(operation_id):
                    verdict = await self._judge_goal(operation_id, model_id)
                    if verdict == goal_judge.STOP:
                        self._write_dim(self.translator.t("chat.goal-met"))
                        return TerminalReason.COMPLETED
                stretches_used += 1
                self._write_dim(
                    self.translator.t(
                        "chat.auto-continue",
                        index=stretches_used,
                        total=self._AUTO_CONTINUES,
                    )
                )
                self._remember(
                    Message(role="user", content=self.translator.t("chat.continue-note"))
                )
                rounds_left = self._max_tool_rounds()
                continue
            rounds_left -= 1
            self._drain_steering()
            self._drain_background()
            self._count_goal_step()
            try:
                response = await self._request_chat_completion(
                    operation_id, model_id, reasoning, self._tool_specs()
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
                            operation_id, model_id, reasoning, self._tool_specs()
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
            self._count_usage(response.usage)
            self._add_cost(
                model_id,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.cached_tokens,
            )
            if not response.tool_calls:
                self._remember(Message(role="assistant", content=response.text))
                self._trim_chat_history()
                if response.text.strip():
                    self._end_live_answer()
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
                if (
                    tool_calls_made
                    and completion_judges < _MAX_COMPLETION_JUDGES
                    and self._operation_goal(operation_id)
                ):
                    # work happened and the model says it is done: only the goal
                    # judge may agree, otherwise the turn ends on a promise
                    completion_judges += 1
                    verdict = await self._judge_goal(operation_id, model_id)
                    if verdict == goal_judge.STOP:
                        self._write_dim(self.translator.t("chat.goal-met"))
                        return TerminalReason.COMPLETED
                    if verdict is None:
                        # a broken judge never stalls a run: fall back to accepting
                        return TerminalReason.COMPLETED
                    # CONTINUE or unclear: no sign-off without a clear STOP
                    self._write_dim(self.translator.t("chat.goal-not-met"))
                    self._remember(
                        Message(
                            role="user",
                            content=self.translator.t("chat.goal-not-met-note"),
                        )
                    )
                    continue
                return TerminalReason.COMPLETED
            if response.text.strip():
                self._remember(Message(role="assistant", content=response.text))
                # reasoning between tool calls is narrative, not an answer: it
                # folds into the turn's «thinking» block so the dialogue stays
                self._end_live_answer()
                self._write_dim(response.text.strip(), process=True)
            # every call of the round runs even when stopping: a stop is worth
            # nothing until the results are in and checked
            for message in await self._run_round(list(response.tool_calls)):
                tool_calls_made += 1
                self._remember(message)
            self._trim_chat_history()
            if self._stop_requested and not self._stop_note_sent:
                self._stop_note_sent = True
                self._remember(Message(role="user", content=self.translator.t("chat.stop-note")))
            self._note_stagnation()
            if self._finding_count() > findings_before:
                findings_before = self._finding_count()
                self._progress.finding_seen()
        # every stretch is spent without the model concluding: one closing answer
        try:
            final = await self._request_chat_completion(operation_id, model_id, reasoning, ())
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            self._write_line(f"[error] {exc}")
            return TerminalReason.MODEL_ERROR
        if final is None:
            return TerminalReason.DENIED
        self._count_usage(final.usage)
        self._add_cost(
            model_id,
            final.usage.prompt_tokens,
            final.usage.completion_tokens,
            final.usage.cached_tokens,
        )
        self._remember(Message(role="assistant", content=final.text))
        self._trim_chat_history()
        if final.text.strip():
            self._write_ai_message(final)
        return TerminalReason.CANCELLED if self._stop_requested else TerminalReason.ROUNDS


    def _adopt_goal(self, text: str) -> None:
        """Take the task as the goal when none was set; the operator can override.

        A run without a goal is judged by the round counter alone, which cannot
        tell "still working" from "done" — so the task's own wording becomes the
        condition, marked as derived.
        """
        if self.operation is None:
            return
        if self._operation_goal(self.operation.id):
            return
        objective = (self.operation.objective or "").strip()
        derived = objective or " ".join(text.split())[:300]
        if not derived:
            return
        self.operation = self.service.set_goal(self.operation.id, derived, automatic=True)
        self._write_dim(self.translator.t("goal.adopted", goal=derived))
        if self.goals.current(self.operation.id) is None:
            # the strip reads the goal service, not the operation field: without
            # a mirrored goal the adopted condition stays invisible
            self.goals.create(self.operation.id, derived)

    def _operation_goal(self, operation_id: str) -> str:
        """The goal from the store — the in-memory copy can be stale."""
        try:
            return self.service.get(operation_id).goal
        except Exception:  # noqa: BLE001 - no operation means no goal
            return ""

    async def _judge_goal(self, operation_id: str, model_id: str) -> str | None:
        """Ask the cheap judge whether the condition holds; None when unsure."""
        goal = self._operation_goal(operation_id)
        if not goal:
            return None
        transcript = [
            str(message.content)[:400]
            for message in self._chat_history[-12:]
            if message.role in ("assistant", "user")
        ]
        system, question = goal_judge.build_prompt(goal, goal_judge.digest(transcript))
        messages = (
            Message(role="system", content=system),
            Message(role="user", content=question),
        )
        # the judge answers one word: a fast model is enough and keeps the main
        # one out of a mechanical call. Unset means the main model, as before.
        fast = self.settings.get_value("model.fast") or model_id
        answer = await self._request_chat_completion(
            operation_id, fast, None, (), max_tokens=8, messages=messages
        )
        if answer is None:
            self._write_dim(self.translator.t("chat.goal-judge-failed"))
            return None
        # a mumbled answer is not a sign-off: report it apart from "unavailable"
        return goal_judge.parse_verdict(answer.text) or "unclear"

    async def _settle(self, operation_id: str, model_id: str, reasoning: str | None) -> None:
        """One write-only round: persist what the turn found, without new exploration.

        Runs even when the graph stayed empty — that is exactly the turn whose
        findings would otherwise die with the context window.
        """
        tools = tuple(
            spec
            for spec in self._CHAT_TOOL_SPECS
            if spec.name in ("record_fact", "close_intent", "propose_finding", "memory_learn")
        )
        self._remember(Message(role="user", content=self.translator.t("chat.settle-prompt")))
        try:
            response = await self._request_chat_completion(operation_id, model_id, reasoning, tools)
        except Exception:  # noqa: BLE001 - the closing summary still has to happen
            return
        if response is None:
            return
        self._count_usage(response.usage)
        for message in await self._run_round(list(response.tool_calls)):
            self._remember(message)
        if response.text.strip():
            self._remember(Message(role="assistant", content=response.text))

    def _add_cost(
        self,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> None:
        """Accumulate the run's spend per model; an unknown tariff stays unknown.

        Cached prompt tokens are billed at a discount the provider does not
        disclose, so they are counted at zero rather than at full price: the
        ⚡ share in the bar says how much of the prompt that was.
        """
        self._run_costs.setdefault(model_id, 0.0)
        try:
            model, _provider = self.models.resolve_model(model_id)
        except Exception:  # noqa: BLE001 - an unpriced model is not an error
            self._run_cost_unknown.add(model_id)
            return
        if not model.price_known:
            self._run_cost_unknown.add(model_id)
            return
        billed_prompt = max(0, prompt_tokens - cached_tokens)
        self._run_costs[model_id] += model.estimate_cost(billed_prompt, completion_tokens)

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

    def _normalize_prlctl(self, command: str) -> str:
        """`prlctl exec <uuid> <cmd>` typed by hand loses its quoting on the
        far side; rebuild it through the relay helper so it arrives intact."""
        from samurai_ai.execution.relay import relay_command

        try:
            tokens = __import__("shlex").split(command)
        except ValueError:
            return command
        if len(tokens) < 3 or tokens[0] != "prlctl" or tokens[1] != "exec":
            return command
        # prlctl exec <uuid> <tail...>: hand the whole tail to bash -lc
        tail = tokens[2:]
        if tail and tail[0] == "--":
            tail = tail[1:]
        if not tail:
            return command
        relay = f"prlctl exec {tokens[2]}"
        return relay_command(relay, tail[0], tail[1:])

    def _chat_tool_read_only(self, name: str) -> bool:
        tool_id = self._CHAT_TOOL_IDS.get(name)
        return tool_id is not None and self._tool_is_read_only(tool_id)

    async def _preapprove(self, calls: list[ToolCall]) -> dict[str, bool]:
        """One approval screen for the whole round, not one modal per command.

        The commands of a single model answer share risk and shape, so they are
        approved together; a denied one is remembered as False and answers as
        denied without stopping its siblings.
        """
        self._round_approvals.clear()
        asking: list[tuple[str, str]] = []
        batched: list[str] = []
        first: str = ""
        first_risk: str = ""
        first_call: tuple[str, CommandRisk] = ("", CommandRisk())
        allowed_by_id: dict[str, bool] = {}
        mode = self._permission_mode
        for call in calls:
            tool_id = self._CHAT_TOOL_IDS.get(call.name)
            command = str(call.arguments.get("command", "")).strip()
            blocked_by_plan = self._plan_mode and not self._tool_is_read_only(tool_id or "")
            if tool_id is None or not command or blocked_by_plan:
                continue
            if self.execution.timeout_repeat(
                self.operation.id if self.operation else "", tool_id, command
            ) is not None:
                # do not ask the operator to approve a probe that will only be
                # refused by the anti-repeat guard a moment later
                allowed_by_id[call.id] = False
                continue
            risk = classify_command(command, str(self._cwd))
            decision = decide(
                self.settings.permission_rules(),
                tool_id,
                command,
                mode,
                risk,
                reusable=self._reusable_approval(tool_id),
            )
            if decision.is_denied:
                allowed_by_id[call.id] = False
                continue
            if decision.action == "ask":
                asking.append((_short_tool(tool_id, command), f"r3 · {risk.label()}"))
                batched.append(call.id)
                if first == "":
                    # the screen shows this command in full; the batch list is
                    # the tail of the same round, not a different decision
                    first, first_risk = command, risk.label() or "r3"
                    first_call = (tool_id, risk)
                continue
            allowed_by_id[call.id] = True
        if not asking:
            self._round_approvals.update(allowed_by_id)
            return allowed_by_id
        allowed = bool(
            await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    first,
                    first_risk,
                    meta=self._approval_meta(first_call[0], first, first_call[1], batch=asking),
                    batch_items=asking,
                )
            )
        )
        for call_id in batched:
            allowed_by_id[call_id] = allowed
        self._round_approvals.update(allowed_by_id)
        return allowed_by_id

    async def _run_round(self, calls: list[ToolCall]) -> list[Message]:
        """Independent calls of one answer run together, capped and ordered.

        Sequential awaits made a four-command round cost four commands' time.
        Results are collected in call order so the model reads a deterministic
        transcript whatever order they finish in.
        """
        calls = [_bounded_call(call) for call in calls]
        await self._preapprove(calls)
        gate = asyncio.Semaphore(self._PARALLEL_LIMIT)

        async def one(call: ToolCall) -> Message:
            async with gate:
                return await self._run_chat_tool(call)

        return list(await asyncio.gather(*(one(call) for call in calls)))

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
        task = str(call.arguments.get("task", "") or "").strip() or None
        run_id = await self.spawn_subagent(
            instruction, specialization=specialization, task_id=task
        )
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

    async def _maybe_offer_relay(self, text: str) -> None:
        """A task that names a relay deserves one operator tap to adopt it.

        Once per operation: without this, the model hand-builds the prefix into
        every command and loses the session to quoting (HTB Takeoff, 37 failed
        calls).
        """
        if self.operation is None or self.operation.relay:
            return
        if self.operation.id in self._relay_offered:
            return
        candidate = None
        for pattern in self._RELAY_CANDIDATE_RES:
            match = pattern.search(text)
            if match:
                candidate = match.group(0)
                break
        if candidate is None:
            if self._BARE_RELAY_RE.search(text):
                self._relay_offered.add(self.operation.id)
                self._write_dim(self.translator.t("relay.detected-bare"), process=False)
            return
        self._relay_offered.add(self.operation.id)
        choice = await self.push_screen_wait(
            PickerScreen(
                self.translator,
                "relay.detected",
                "relay.detected-hint",
                "side.list-empty",
                [
                    ("use", self.translator.t("relay.use", relay=candidate), ""),
                    ("skip", self.translator.t("relay.skip"), ""),
                ],
            )
        )
        if choice != "use" or self.operation is None:
            return
        try:
            self.operation = self.service.set_relay(self.operation.id, candidate)
        except Exception as exc:  # noqa: BLE001 - shown, not raised
            self._write_line(f"[relay] {exc}")
            return
        self._write_dim(self.translator.t("relay.set", relay=candidate), process=False)
        self._relay_canary(candidate)
        self._refresh_chrome()

    def _remote_scope(self) -> bool:
        """True when the scope names a target loopback cannot explain."""
        if self.operation is None:
            return False
        from ipaddress import ip_address

        for rule in self.operation.scope.includes:
            host = rule.split("/", 1)[0].lstrip("*.")
            if "://" in rule:
                host = rule.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
            try:
                if ip_address(host).is_loopback:
                    continue
            except ValueError:
                pass
            return True
        return False

    def _task_targets(self, text: str) -> list[str]:
        """Hosts and networks the task names that the scope does not cover yet."""
        from samurai_ai.tui.killchain import hosts_mentioned

        assert self.operation is not None
        scope = self.operation.scope
        cidrs = list(dict.fromkeys(self._CIDR_RE.findall(text)))
        net_addresses = {cidr.split("/")[0] for cidr in cidrs}
        targets: list[str] = []
        for host in hosts_mentioned(text):
            if host in net_addresses:
                continue  # the CIDR spelling is the fuller statement
            targets.append(host)
        targets.extend(cidrs)
        return [
            target
            for target in targets
            if target not in scope.hosts
            and not scope.covers(target)
            and not self._is_loopback(target)
        ]

    @staticmethod
    def _is_loopback(target: str) -> bool:
        from ipaddress import ip_address

        try:
            return ip_address(target.split("/")[0]).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _wildcard_offer(targets: list[str]) -> tuple[str, int] | None:
        """A shared parent covering 3+ hosts deserves one wildcard rule instead."""
        from collections import Counter

        parents = Counter(
            ".".join(target.split(".")[-2:])
            for target in targets
            if not target[0].isdigit() and "." in target
        )
        if not parents:
            return None
        parent, count = parents.most_common(1)[0]
        return (f"*.{parent}", count) if count >= 3 else None

    async def _maybe_offer_scope(self, text: str) -> None:
        """Targets named in the task land in the scope without a popup.

        The operator wrote them into the task — that is the authorization; a
        picker in the way of the first turn only got dismissed unread. Three or
        more targets under one parent domain fold into a wildcard rule, so a
        subfinder-scale text does not bloat the contract.
        """
        if self.operation is None or self.operation.id in self._scope_offered:
            return
        if len(self._chat_history) > 2:
            # only the task message earns the extension; a host named
            # mid-conversation is a mention, not a widening request
            return
        targets = self._task_targets(text)
        if not targets:
            return
        self._scope_offered.add(self.operation.id)
        self._fresh_task_text = None
        wildcard = self._wildcard_offer(targets)
        if wildcard is not None:
            rule, _count = wildcard
            covered = {
                target
                for target in targets
                if not target[0].isdigit() and "." in target
                and (target == rule[2:] or target.endswith(rule[1:]))
            }
            rest = [target for target in targets if target not in covered]
            self._extend_scope([rule, *rest])
            return
        self._extend_scope(targets)

    def _extend_scope(self, added: list[str]) -> None:
        """Widen the contract and say what landed; invalid entries are skipped."""
        assert self.operation is not None
        scope = self.operation.scope
        valid: list[str] = []
        for target in added:
            try:
                ScopeContract(includes=(target,))  # parse and normalize one rule
            except Exception:  # noqa: BLE001 - an unparseable name is skipped, not fatal
                continue
            valid.append(target)
        if not valid:
            return
        scope = self.operation.scope
        updated = ScopeContract(
            includes=(*scope.includes, *valid), excludes=scope.excludes, hosts=scope.hosts
        )
        self.operation = self.service.replace_scope(self.operation.id, updated)
        self._write_dim(
            self.translator.t("scope.extended", rules=", ".join(valid)), process=False
        )
        self._refresh_chrome()

    async def _run_check_scope_tool(self, call: ToolCall) -> Message:
        """The model asks before touching: is this resource inside the scope?"""
        target = str(call.arguments.get("target", "") or "").strip()
        if self.operation is None:
            return Message(role="user", content="[tool check_scope] no open operation")
        if not target:
            return Message(role="user", content="[tool check_scope] empty target")
        scope = self.operation.scope
        if "/" in target and "://" not in target:
            decision = scope.evaluate_network(target)
        else:
            decision = scope.evaluate(target)
        rule = f" (rule {decision.matched_rule})" if decision.matched_rule else ""
        return Message(
            role="user",
            content=(
                f"[tool check_scope] {target}: {decision.reason.value}{rule} — "
                f"{decision.detail}"
            ),
        )

    async def _run_request_scope_tool(self, call: ToolCall) -> Message:
        """Mid-run widening: the agent extends the scope itself, journaled."""
        if self.operation is None:
            return Message(role="user", content="[tool add_scope] no open operation")
        raw = call.arguments.get("targets") or []
        targets = [str(item).strip() for item in raw if str(item).strip()][:50]
        if not targets:
            return Message(role="user", content="[tool add_scope] no targets given")
        fresh = [target for target in targets if not self.operation.scope.covers(target)]
        if not fresh:
            return Message(
                role="user", content="[tool add_scope] all targets already in scope"
            )
        wildcard = self._wildcard_offer(fresh)
        if wildcard is not None:
            rule, _count = wildcard
            covered = {
                target
                for target in fresh
                if not target[0].isdigit() and "." in target
                and (target == rule[2:] or target.endswith(rule[1:]))
            }
            rest = [target for target in fresh if target not in covered]
            added = [rule, *rest]
        else:
            added = fresh
        self._extend_scope(added)
        return Message(
            role="user",
            content=(
                f"[tool add_scope] added: {', '.join(added)} — the operator can "
                "revert any rule with /scope rm"
            ),
        )

    async def _run_set_relay_tool(self, call: ToolCall) -> Message:
        """The model fixes its own transport instead of hand-rolling prefixes."""
        prefix = str(call.arguments.get("prefix", "") or "").strip()
        if self.operation is None:
            return Message(role="user", content="[tool set_relay] no open operation")
        try:
            self.operation = self.service.set_relay(self.operation.id, prefix)
        except Exception as exc:  # noqa: BLE001 - the model gets the reason
            return Message(role="user", content=f"[tool set_relay] rejected: {exc}")
        relay = self.operation.relay
        if relay:
            self._write_dim(self.translator.t("relay.set", relay=relay), process=False)
            self._relay_canary(relay)
        else:
            self._write_dim(self.translator.t("relay.cleared"), process=False)
        self._refresh_chrome()
        return Message(
            role="user",
            content=(
                f"[tool set_relay] relay is now: {relay or 'off'}; write plain "
                "far-side commands, the harness quotes them for the relay"
            ),
        )

    async def _run_provision_tool(self, call: ToolCall) -> Message:
        """The model asks for a missing tool; the operator sees the plan first."""
        tool_id = str(call.arguments.get("tool_id", "") or "").strip()
        if self.operation is None:
            return Message(role="user", content="[tool provision_tool] no open operation")
        provisioner = getattr(self, "provisioner", None)
        if provisioner is None:
            return Message(role="user", content="[tool provision_tool] provisioning is off")
        from samurai_ai.domain.provision import ProvisionError, ProvisionLockedError
        from samurai_ai.services.provisioner import Provisioner

        try:
            profile = await asyncio.to_thread(provisioner.detector.detect, refresh=True)
            plan = provisioner.plan(tool_id, profile)
        except ProvisionError as exc:
            return Message(
                role="user",
                content=(
                    f"[tool provision_tool] {tool_id} cannot be installed: {exc}"
                    + (
                        f" (manual: {getattr(exc, 'manual', '')})"
                        if getattr(exc, "manual", "")
                        else ""
                    )
                ),
            )
        journal = self.query_one("#journal", BlockJournal)
        for line in Provisioner.describe(plan):
            journal.write(Text(f"[scope] {line}", style=self.paint("accent")))
        if provisioner.needs_decision(tool_id):
            self._write_dim(self.translator.t("provision.retry"), process=False)
        approval = self.kernel.request_approval(
            self.operation.id,
            f"provision {plan.tool_id} v{plan.recipe_version} via {plan.manager}",
            RiskClass.R3,
            reason="system package installation from the reviewed catalogue",
            requested_by="agent",
        )
        allowed = await self.push_screen_wait(
            AccessPromptScreen(
                self.translator,
                f"provision {plan.tool_id}: {' '.join(plan.commands[0]) if plan.commands else ''}",
                f"r3 · {plan.platform}",
                batch_items=[(" ".join(argv), "r3") for argv in plan.commands],
            )
        )
        self.kernel.approve(approval.id, decided_by=("operator" if allowed else "operator"))
        if not allowed:
            return Message(role="user", content=f"[tool provision_tool] {tool_id} declined")
        try:
            result = await asyncio.to_thread(
                provisioner.run, self.operation.id, tool_id, profile
            )
        except ProvisionLockedError as exc:
            return Message(role="user", content=f"[tool provision_tool] {exc}")
        except ProvisionError as exc:
            return Message(role="user", content=f"[tool provision_tool] {tool_id}: {exc}")
        if result.status.value != "ready":
            self._write_line(f"[error] provision {tool_id}: {result.error}")
            return Message(
                role="user",
                content=f"[tool provision_tool] {tool_id} failed: {result.error}",
            )
        version = provisioner.state(tool_id).installed_version
        self._write_dim(self.translator.t("provision.ready", tool=tool_id), process=False)
        self._refresh_chrome()
        return Message(
            role="user",
            content=(
                f"[tool provision_tool] {tool_id} installed and verified "
                f"({version or 'ready'}); run it once more — do not provision again"
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
            plan = {
                key: str(call.arguments.get(key) or "").strip()[:200]
                for key in ("next_test", "stop_condition")
                if str(call.arguments.get(key) or "").strip()
            }
            if plan:
                intent = self.graph.annotate_intent(intent.id, plan)
        except GraphError as exc:
            return Message(role="user", content=f"[tool open_intent] {exc}")
        self._graph_used = True
        self._note_structure_written()
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
        self._note_structure_written()
        # a confirmed fact changes the vector map: collected here, the
        # strategist reviews them once the turn is over (never mid-turn)
        self._turn_facts.append(summary)
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
        tasks: list[dict[str, object]] = []
        for item in raw_tasks[:12]:
            if isinstance(item, str):
                title_text = item.strip()
                if title_text:
                    tasks.append({"title": title_text})
            elif isinstance(item, dict):
                title_text = str(item.get("title", "")).strip()
                if not title_text:
                    continue
                entry: dict[str, object] = {"title": title_text}
                if item.get("depends_on"):
                    entry["depends_on"] = [
                        str(dep) for dep in item["depends_on"] if str(dep).strip()
                    ]
                if item.get("risk_class"):
                    entry["risk_class"] = str(item["risk_class"])
                tasks.append(entry)
        if not tasks:
            return Message(role="user", content="[tool propose_plan] no tasks given")
        self._proposed_plan = (title, tasks)
        block = Text()
        block.append("≡ " + title, style=self.paint("bold $accent"))
        for index, task in enumerate(tasks, start=1):
            label = str(task["title"])
            if task.get("depends_on"):
                raw_deps = task["depends_on"]
                deps = [str(d) for d in raw_deps] if isinstance(raw_deps, list) else []
                label += " ← " + ", ".join(deps)
            if task.get("risk_class"):
                label += f" [{task['risk_class']}]"
            block.append(f"\n  {index}. {label}", style=self.paint("$text"))
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


    async def _run_remember_secret_tool(self, call: ToolCall) -> Message:
        """A found credential goes into the vault and shows up in the sidebar."""
        name = str(call.arguments.get("name", "")).strip().lower()
        value = str(call.arguments.get("value", ""))
        if not name or not value:
            return Message(role="user", content="[tool remember_secret] name and value required")
        if self.operation is None:
            return Message(role="user", content="[tool remember_secret] no operation open")
        try:
            self.vault.add(name, value, self.operation.id, created_by="agent")
        except Exception as exc:  # noqa: BLE001 - fed back to the model
            return Message(role="user", content=f"[tool remember_secret] {exc}")
        refresh = getattr(self, "_refresh_side_blocks", None)
        if refresh is not None:
            refresh()
        self._write_dim(self.translator.t("side.secret-saved", name=name), process=False)
        return Message(
            role="user",
            content=f"[tool remember_secret] stored as {{{{{name}}}}} (operation vault)",
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
        self._note_structure_written()
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
        """Run one shell tool, handing it back to the operator when it drags.

        A sweep that legitimately takes ten minutes used to freeze the whole
        turn behind a blocking await. Past the foreground window the run is
        parked: the model is told it is still working, the loop carries on, and
        the result is delivered when it lands.
        """
        tool_id = self._CHAT_TOOL_IDS.get(call.name)
        command = str(call.arguments.get("command", "")).strip()
        # The model still writes `prlctl exec <uuid> -- bash -c '...'` by hand
        # and loses to the re-split; rebuild the tail like the relay does.
        if tool_id == "shell.exec":
            command = self._normalize_prlctl(command)
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
        if tool_id == "shell.exec" and self.operation is not None:
            repeated = self.execution.timeout_repeat(self.operation.id, tool_id, command)
            if repeated is not None:
                note = self.translator.t("chat.timeout-repeat", run=repeated.id)
                self._write_dim(note, process=False)
                return Message(role="user", content=f"[tool {call.name}] {note}")
        risk = classify_command(command, str(self._cwd))
        badge = f"  [{risk.label()}]" if not risk.empty else ""
        # the running command lives in the status bar; the finished one becomes
        # its own one-line block, so a repeat in the buffer is noise
        self._current_tool = _short_tool(tool_id, command) + badge
        mode = self._permission_mode
        # A round approves all of its commands on one screen; a call made on its
        # own (a test, a retry) still asks for itself here.
        hint = self._round_approvals.pop(call.id, None)
        if hint is None:
            decision = decide(
                self.settings.permission_rules(),
                tool_id,
                command,
                mode,
                risk,
                reusable=self._reusable_approval(tool_id),
            )
            if decision.is_denied:
                self._write_dim(
                    self.translator.t("chat.tool-denied", command=command), process=False
                )
                return Message(role="user", content=f"[tool {call.name}] denied by rule")
            if decision.action == "ask" and not await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    f"{tool_id}: {command}",
                    f"r3{badge}" if risk.empty else f"r3 · {risk.label()}",
                )
            ):
                self._write_dim(
                    self.translator.t("chat.tool-denied", command=command), process=False
                )
                return Message(
                    role="user",
                    content=f"[tool {call.name}] denied by the operator",
                )
            hint = True
        if not hint:
            self._write_dim(self.translator.t("chat.tool-denied", command=command), process=False)
            return Message(role="user", content=f"[tool {call.name}] denied by the operator")
        call_label = f"{call.name}: {_short_tool(tool_id, command)}"
        # the id is written from the worker thread, so it travels in a plain box
        holder: dict[str, str] = {}
        self._running_tools += 1
        task = asyncio.ensure_future(
            self._execute_shell(call.name, tool_id, command, mode, holder)
        )
        task.add_done_callback(self._tool_finished)
        try:
            result, approval_id = await asyncio.wait_for(
                asyncio.shield(task), self._FOREGROUND_WINDOW
            )
        except ToolScopeError as exc:
            return await self._offer_scope(exc, call, tool_id, command, mode)
        except TimeoutError:
            run_id = holder.get("run", "")
            self._park(task, call_label)
            handle = f" run={run_id}" if run_id else ""
            self._write_dim(self.translator.t("chat.tool-background", command=call_label))
            return Message(
                role="user",
                content=(
                    f"[tool {call.name}] still running in the background{handle}: "
                    "the result arrives as another tool result later. Keep working; "
                    "do not re-run the same command."
                ),
            )
        return self._shell_result_message(call.name, tool_id, command, result, approval_id)

    async def _offer_scope(
        self,
        exc: ToolScopeError,
        call: ToolCall,
        tool_id: str,
        command: str,
        mode: Any,
    ) -> Message:
        """The guard refused: show what it was doing, offer it, retry once.

        A refusal is the boundary working, so it reads as information — and the
        operator gets one key to authorize the resource instead of paying a
        model round trip to retype it.
        """
        journal = self.query_one("#journal", BlockJournal)
        journal.write(Text(f"[scope] {exc}", style=self.paint("accent")))
        literal = getattr(exc, "literal", "")
        if not literal:
            return Message(role="user", content=f"[tool {call.name}] out of scope: {exc}")
        from samurai_ai.tui.screens.scope_ask import ScopeAskScreen

        allowed = await self.push_screen_wait(
            ScopeAskScreen(self.translator, literal, getattr(exc, "detail", ""))
        )
        if not allowed:
            return Message(
                role="user",
                content=f"[tool {call.name}] out of scope (operator skipped): {exc}",
            )
        self._extend_scope([literal.split("://")[-1].split("/")[0]])
        holder: dict[str, str] = {}
        retry = asyncio.ensure_future(
            self._execute_shell(call.name, tool_id, command, mode, holder)
        )
        retry.add_done_callback(self._tool_finished)
        self._running_tools += 1
        try:
            result, approval_id = await asyncio.wait_for(
                asyncio.shield(retry), self._FOREGROUND_WINDOW
            )
        except TimeoutError:
            run_id = holder.get("run", "")
            self._park(retry, f"{call.name}: {_short_tool(tool_id, command)}")
            handle = f" run={run_id}" if run_id else ""
            return Message(
                role="user",
                content=(
                    f"[tool {call.name}] added to the scope and still running in the "
                    f"background{handle}: the result arrives later."
                ),
            )
        except ToolScopeError as again:
            return Message(role="user", content=f"[tool {call.name}] out of scope: {again}")
        except Exception as again:  # noqa: BLE001 - fed back to the model
            self._write_line(f"[error] {again}")
            return Message(role="user", content=f"[tool {call.name}] error: {again}")
        return self._shell_result_message(call.name, tool_id, command, result, approval_id)

    async def _execute_shell(
        self,
        name: str,
        tool_id: str,
        command: str,
        mode: Any,
        holder: dict[str, str],
    ) -> tuple[Any, str | None]:
        """The blocking call, plus its approval dance, off the event loop."""
        approval_id: str | None = None

        def note(run_id: str) -> None:
            holder["run"] = run_id

        try:
            result = await asyncio.to_thread(
                self.execution.run_tool,
                self.operation.id if self.operation else "",
                tool_id,
                {"command": command},
                on_started=note,
            )
        except ApprovalRequiredError as exc:
            self.kernel.approve(exc.approval_id, decided_by=mode.value)
            approval_id = exc.approval_id
            result = await asyncio.to_thread(
                self.execution.run_tool,
                self.operation.id if self.operation else "",
                tool_id,
                {"command": command},
                approval_id=approval_id,
                on_started=note,
            )
        return result, approval_id

    def _tool_finished(self, _task: asyncio.Future[Any]) -> None:
        self._running_tools = max(0, self._running_tools - 1)

    def _park(self, task: asyncio.Future[Any], label: str) -> None:
        """Collect a still-running tool into the background queue when it lands."""

        def _deliver(done: asyncio.Future[Any]) -> None:
            try:
                result, approval_id = done.result()
            except ToolScopeError as exc:
                journal = self.query_one("#journal", BlockJournal)
                journal.write(Text(f"[scope] {exc}", style=self.paint("accent")))
                self._background_results.append(
                    Message(role="user", content=f"[tool {label}] out of scope: {exc}")
                )
                return
            except Exception as exc:  # noqa: BLE001 - one tool must not lose the turn
                self._write_line(f"[error] {exc}")
                self._background_results.append(
                    Message(role="user", content=f"[tool {label}] error: {exc}")
                )
                return
            tool_name, _, short = label.partition(": ")
            tool_id = self._CHAT_TOOL_IDS.get(tool_name, tool_name)
            self._background_results.append(
                self._shell_result_message(
                    tool_name, tool_id, short, result, approval_id, background=True
                )
            )

        task.add_done_callback(_deliver)

    def _drain_background(self) -> None:
        """Finished background runs land before the next model call, in order."""
        while self._background_results:
            message = self._background_results.pop(0)
            self._remember(message)
            self._write_dim(
                self.translator.t("chat.tool-background-done", command=message.content[:60])
            )

    def _shell_result_message(
        self,
        name: str,
        tool_id: str,
        command: str,
        result: Any,
        approval_id: str | None,
        *,
        background: bool = False,
    ) -> Message:
        """The journal block and the model-facing result of a finished run."""
        exit_code = result.run.exit_code
        self._progress.last_stuck = None if self._graph_used else self._progress.record(command)
        # The run lands as a collapsible block; a failure opens on its own.
        self._write_tool_block(
            self.translator.t(
                "chat.tool-collapsed",
                command=_short_tool(tool_id, command),
                code=exit_code,
                code_mark="" if exit_code == 0 else f" ✗{exit_code}",
                lines=len(result.stdout.strip().splitlines()),
            ),
            exit_code,
            result.stdout,
            result.stderr,
            run_id=result.run.id,
        )
        stdout = result.stdout[: self._TOOL_OUTPUT_FEEDBACK_LIMIT]
        stderr = result.stderr[:1000]
        mark = " (background)" if background else ""
        return Message(
            role="user",
            content=(
                f"[tool result {name} exit={exit_code}]{mark}\nstdout:\n{stdout}\nstderr:\n{stderr}"
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
        # a long command must not wrap the header: one line, cut with an ellipsis
        columns = getattr(self.size, "width", 100) if hasattr(self, "size") else 100
        width = max(40, columns - 28)
        if len(title) > width:
            title = title[: width - 1] + "…"
        failed = exit_code not in (0, None)
        if failed and not (self.operation and self.operation.relay):
            warn = getattr(self, "_relay_warning", None)
            if warn is not None:
                warn()
        if failed:
            error = (stderr.strip() or stdout.strip()).splitlines()
            tail = error[-1] if error else f"exit {exit_code}, no output"
            title = title[: max(20, width - 30)] + "  ← " + tail[:26]
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
