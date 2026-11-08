import asyncio
import json
import pathlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, cast

from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Horizontal, Vertical
from textual.timer import Timer
from textual.widgets import (
    ContentSwitcher,
    Input,
    OptionList,
    Static,
    Tab,
    Tabs,
)
from textual.widgets.option_list import Option

from samurai_ai import __version__
from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.agents.provider import Message
from samurai_ai.config import serve_socket_path
from samurai_ai.domain.access import AccessError, Role
from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.constraints import ConstraintKind
from samurai_ai.domain.execution import SecretRedactor, ToolDefinition, ToolRun
from samurai_ai.domain.goal import Goal, GoalError
from samurai_ai.domain.graph import NodeKind, NodeState
from samurai_ai.domain.kernel import (
    ApprovalStatus,
    Artifact,
    GateStatus,
    TaskStatus,
)
from samurai_ai.domain.lessons import LessonMode
from samurai_ai.domain.operations import Operation, OperationEvent, OperationMode
from samurai_ai.domain.permission import (
    PermissionMode,
    PermissionRuleError,
    next_mode,
    parse_mode,
)
from samurai_ai.domain.retest import RetestVerdict
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.domain.teamwork import NoteTarget, OperationLevel, TeamworkError
from samurai_ai.domain.traffic import TrafficRole
from samurai_ai.i18n import Translator
from samurai_ai.services.access import AccessService
from samurai_ai.services.chat import ChatHistoryService
from samurai_ai.services.constraints import ConstraintService
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.goals import GoalService
from samurai_ai.services.graph import GraphService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.lessons import LessonService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.retests import RetestService
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.services.skills import SkillService
from samurai_ai.services.teamwork import TeamworkService
from samurai_ai.services.traffic import TrafficService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.access import AccessRepository
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.chat import ChatHistoryRepository
from samurai_ai.storage.constraints import ConstraintRepository
from samurai_ai.storage.graph import GraphRepository
from samurai_ai.storage.lessons import LessonRepository
from samurai_ai.storage.retests import RetestRepository
from samurai_ai.storage.swarm import SwarmRunRepository
from samurai_ai.storage.teamwork import TeamworkRepository
from samurai_ai.storage.traffic import TrafficRepository
from samurai_ai.tui import keys, preferences
from samurai_ai.tui.boards import BoardController
from samurai_ai.tui.chat import ChatController
from samurai_ai.tui.commands import CommandSpec, command_path, find_command
from samurai_ai.tui.graph_board import GraphBoardController
from samurai_ai.tui.hunt import REFRESH_SECONDS as HUNT_REFRESH_SECONDS
from samurai_ai.tui.hunt import HuntController
from samurai_ai.tui.journal import (  # noqa: F401
    BlockJournal,
    InspectSummaryBlock,
    ToolBlock,
    UserBlock,
    entity_span,
)
from samurai_ai.tui.keymap import KeyMapController
from samurai_ai.tui.palette import CommandProvider
from samurai_ai.tui.progress import Stagnation, TerminalReason
from samurai_ai.tui.screens import (
    ADD_CUSTOM_ID,
    REASONING_LEVELS,
    AccessPromptScreen,
    ArgumentScreen,
    CustomEndpointScreen,
    HelpScreen,
    InspectorScreen,
    LanguagePromptScreen,
    ModelPickerScreen,
    NewOperationScreen,
    ProjectPickerScreen,
    Section,
    SettingsScreen,
    TrustDirectoryScreen,
)
from samurai_ai.tui.screens.attach import AttachScreen
from samurai_ai.tui.screens.sessions import SessionDetailScreen, SessionsScreen
from samurai_ai.tui.sessions import SessionController
from samurai_ai.tui.theme import (
    BUILTIN,
    DEFAULT_THEME,
    TOKENS,
    build_theme,
    builtin_theme,
    load_theme_files,
    themes_directory,
)
from samurai_ai.tui.themes import ThemeController
from samurai_ai.tui.widgets import (
    HistoryInput,
    OperationTab,
    PlusButton,
    ViewTabs,
)

_PERMISSION_MODE_KEY = "tui.permission_mode"

_TOKEN_REFERENCE = re.compile(r"\$([a-z-]+)")
_BARE_TOKEN = re.compile(
    r"(?<![\w-])(?:" + "|".join(sorted(TOKENS, key=len, reverse=True)) + r")(?![\w-])"
)
_YOLO_LEGACY_KEY = "tui.yolo"


#: Reachable without a role: otherwise nobody could ever sign in.
_OPEN_COMMANDS = frozenset({"/login", "/whoami"})


class SamuraiApp(
    HuntController,
    ChatController,
    SessionController,
    BoardController,
    GraphBoardController,
    ThemeController,
    KeyMapController,
    App[None],
):
    COMMANDS = {CommandProvider}

    #: how much of an attached file rides into the message
    _ATTACH_LIMIT = 200_000

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
        color: $text;
    }
    .screen--selection {
        background: $border;
        color: $text;
    }
    #topbar {
        height: 2;
        background: $panel;
    }
    #view-tabs {
        width: auto;
        max-width: 45%;
        height: 2;
        background: $panel;
    }
    #topbar-space {
        width: 1fr;
        height: 2;
    }
    #new-tab {
        width: 5;
        height: 2;
        content-align: center middle;
        background: $panel-alt;
        color: $success;
        text-style: bold;
    }
    #new-tab:hover {
        background: $panel-alt;
    }
    #brand {
        width: auto;
        height: 2;
        padding: 0 1;
        text-align: right;
    }
    #body {
        height: 1fr;
    }
    #killchain {
        height: 1;
        padding: 0 1;
        background: $panel-alt;
        display: none;
    }
    #views {
        width: 1fr;
        height: 100%;
    }
    #journal, #view-ops, #view-bounty {
        height: 1fr;
        padding: 0 1;
        border: solid $border;
        background: $surface;
    }
    #side {
        width: 30;
        height: 100%;
        border: solid $border;
        background: $panel;
    }
    #side-workspace {
        height: 2;
        padding: 0 1;
        background: $panel-alt;
        color: $accent;
    }
    #side-access, #side-creds {
        height: auto;
        padding: 1 1 0 1;
    }
    #side-swarm {
        height: auto;
        padding: 1 1 0 1;
    }
    #ops-strip {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    .strip-item {
        width: auto;
        padding: 0 1;
        display: none;
    }
    #welcome {
        padding: 1 2;
        height: auto;
    }
    #thinking {
        height: 1;
        padding: 0 1;
        background: $surface;
        display: none;
    }
    #command-suggestions {
        height: auto;
        max-height: 9;
        border: solid $border;
        background: $panel;
        display: none;
    }
    #command-input {
        height: 3;
        border: solid $accent-alt;
        background: $panel;
    }
    #hint-bar {
        height: 1;
        padding: 0 1;
        background: $panel-alt;
        color: $text-dim;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-dim;
    }
    """

    _VIEW_BY_TAB = {
        "tab-chat": "journal",
        "tab-ops": "view-ops",
        "tab-bounty": "view-bounty",
        "tab-hunt": "view-hunt",
    }
    _TAB_BY_VIEW = {pane: tab for tab, pane in _VIEW_BY_TAB.items()}

    #: Journal tag styles, as token names resolved against the active theme.
    _TAG_TOKENS = {
        "error": ("bold", "danger"),
        "deny": ("", "danger"),
        "allow": ("", "success"),
        "status": ("dim", "text-dim"),
        "scope": ("", "accent"),
        "plan": ("", "accent"),
        "task": ("", "accent"),
        "gate": ("", "warning"),
        "approval": ("", "warning"),
        "finding": ("", "accent-alt"),
        "evidence": ("", "text-faint"),
        "artifact": ("", "text-faint"),
        "snapshot": ("", "text-faint"),
        "tools": ("", "accent-alt"),
        "tool": ("", "accent-alt"),
        "run": ("", "accent-alt"),
        "runs": ("", "accent-alt"),
        "agent": ("", "accent-alt"),
        "agents": ("", "accent-alt"),
        "swarm": ("", "accent-alt"),
        "member": ("", "accent-alt"),
        "memory": ("", "accent"),
        "skill": ("", "accent"),
        "skills": ("", "accent"),
        "playbook": ("", "accent"),
        "playbooks": ("", "accent"),
        "secret": ("", "risk-mid"),
        "secrets": ("", "risk-mid"),
        "asset": ("", "risk-low"),
        "assets": ("", "risk-low"),
        "report": ("", "risk-mid"),
        "model": ("", "accent"),
        "access": ("", "warning"),
        "info": ("dim", "text-dim"),
    }

    BINDINGS = [
        Binding("ctrl+c", "quit_requested", "Copy / stop / quit", show=False),
        # copy without the ladder: the gesture people expect from a terminal
        Binding("ctrl+shift+c", "copy_selection", "Copy selection", show=False, priority=True),
        Binding("ctrl+o", "toggle_journal", "Toggle journal", show=False),
        Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True),
        Binding("f1", "show_help", "Keys", show=False),
        Binding("ctrl+i", "inspect_last_run", "Inspect run", show=False, priority=True),
        Binding("ctrl+g", "open_sessions", "Subagent sessions", show=False, priority=True),
        Binding("ctrl+t", "open_task_board", "Plan board", show=False, priority=True),
        Binding("ctrl+d", "attach_session", "Attach to session", show=False, priority=True),
        Binding("ctrl+f", "open_finding_board", "Findings board", show=False, priority=True),
        Binding("ctrl+s", "steer", "Steer the running turn", show=False, priority=True),
        Binding("ctrl+b", "shell_background", "Background the shell job", show=False),
        Binding("ctrl+e", "expand_output", "Expand collapsed output", show=False, priority=True),
        # priority: the focused input would otherwise keep the key
        Binding("shift+tab", "toggle_plan", "Plan mode", show=False, priority=True),
    ]

    _QUIT_CONFIRM_SECONDS = 2.0

    def __init__(
        self,
        service: OperationService,
        kernel: KernelService,
        settings: SettingsService,
        recovery: RecoveryService,
        execution: ToolExecutionService,
        vault: VaultService,
        agents: AgentRunRepository,
        orchestrator: AgentOrchestrator,
        playbooks: PlaybookService,
        reports: ReportService,
        ctf: CtfService,
        redteam: RedTeamService,
        planning: PlanningService,
        swarms: SwarmRunRepository,
        goals: GoalService,
        memory: CampaignMemoryService,
        skills: SkillService,
        models: ModelService,
        operation_id: str | None = None,
    ) -> None:
        super().__init__()
        self._theme_files: list[str] = []
        self._theme_problems = self._register_themes()
        self.service = service
        self.kernel = kernel
        self.settings = settings
        self.recovery = recovery
        self.execution = execution
        self.vault = vault
        self.agents = agents
        self.orchestrator = orchestrator
        self.playbooks = playbooks
        self.reports = reports
        self.ctf = ctf
        self.redteam = redteam
        self.planning = planning
        self.swarms = swarms
        self.goals = goals
        self.memory = memory
        self.skills = skills
        self.models = models
        self.requested_operation_id = operation_id
        self.operation: Operation | None = None
        self.translator = Translator(settings.get_locale() or "en")
        self._key_problems: list[str] = []
        self.keymap = self._apply_keymap()
        self._key_capture = False
        self._locale_known = settings.get_locale() is not None
        self._last_quit_request = 0.0
        self._history: list[str] = json.loads(settings.get_value("tui.history") or "[]")
        self._history_index: int | None = None
        self._history_draft = ""
        self._cwd = Path.cwd().resolve()
        self._session_trusted: set[str] = set()
        self._dir_trusted: bool | None = None
        self._last_ai_text: str | None = None
        self._last_ai_code: str | None = None
        self._last_user_text: str = ""
        self._open_tabs: list[str] = []
        self._tab_states: dict[str, dict[str, Any]] = {}
        self._pastes: dict[int, str] = {}
        self._projects: list[str] = []
        self._proposed_plan: tuple[str, list[dict[str, object]]] | None = None
        self._ctx_tokens: int = 0
        self._auto_title: str | None = None
        self._tab_activations_seen: int = 0
        self._selection_copy_timer: Timer | None = None
        self._last_auto_copy: str | None = None
        self._killchain_phase: str = ""
        self._live_answer = None
        self._turn_todo: list[tuple[str, str]] = []
        self._chat_history: list[Message] = []
        self._progress = Stagnation()
        self._graph_used = False
        self._last_turn_reason: TerminalReason | None = None
        self.chat_history = ChatHistoryService(
            ChatHistoryRepository(service.repository.database), SecretRedactor()
        )
        self.graph = GraphService(
            service, kernel, GraphRepository(service.repository.database)
        )
        self.lessons = LessonService(
            service, kernel, LessonRepository(service.repository.database), settings
        )
        self.traffic = TrafficService(
            service, kernel, TrafficRepository(service.repository.database)
        )
        self.retests = RetestService(
            service, kernel, RetestRepository(service.repository.database), self.traffic
        )
        self.team = TeamworkService(
            service, AccessRepository(service.repository.database),
            TeamworkRepository(service.repository.database),
        )
        self.access = AccessService(
            settings, vault, AccessRepository(service.repository.database)
        )
        self.constraints = ConstraintService(
            service, ConstraintRepository(service.repository.database)
        )
        # Plan mode is session state on purpose: it is never persisted and only
        # the operator can enter it.
        self._plan_mode = False
        # Messages injected into the running turn instead of waiting for it.
        self._steer_queue: list[str] = []
        self._chat_busy = False
        self._stop_requested = False
        self._stop_note_sent = False
        self._process_block: list[str] = []
        self._thinking_block: ToolBlock | None = None
        self._shell_running = ""
        self._shell_background = False
        # live status: what the running turn is doing right now
        self._run_tokens = 0
        self._run_cost = 0.0
        self._turn_started: float | None = None
        self._current_tool = ""
        self._expandable: list[tuple[str, int | None, str, str]] = []
        self._thinking_timer: Timer | None = None
        self._thinking_depth: int = 0
        self._thinking_frame: int = 0

    _DIM_STYLE = "dim text-dim"

    def _register_themes(self) -> list[str]:
        """Register the built-in presets and any theme files.

        Files are read before the first frame so ``$token`` in CSS and Rich styles
        already resolves; problems are reported into the journal instead of
        stopping the start.
        """
        for name in BUILTIN:
            self.register_theme(builtin_theme(name))
        loaded, problems = load_theme_files(themes_directory())
        for theme in loaded:
            self.register_theme(build_theme(theme.name, theme.palette, dark=theme.dark))
        wanted = preferences.load_theme()
        self.theme = wanted if wanted in self.available_themes else DEFAULT_THEME
        self._theme_files = [theme.name for theme in loaded]
        return problems

    def _apply_keymap(self) -> dict[str, str]:
        """Load the operator's bindings and install them on this instance.

        Textual builds ``BINDINGS`` once per class, so the merged map is replaced
        on the instance instead of mutating the class attribute.
        """
        mapping, problems = keys.load(preferences.keys_path())
        self._key_problems = problems
        labels = {action.name: self.translator.t(action.label_key) for action in keys.ACTIONS}
        self._bindings = BindingsMap(keys.to_bindings(mapping, labels))
        return mapping

    def _refresh_keymap(self) -> None:
        labels = {action.name: self.translator.t(action.label_key) for action in keys.ACTIONS}
        self._bindings = BindingsMap(keys.to_bindings(self.keymap, labels))

    def get_css_variables(self) -> dict[str, str]:
        """Fill tokens Textual's own themes do not define.

        The shell's CSS references tokens a stock theme has never heard of, so a
        theme picked outside our presets would otherwise fail to parse.
        """
        variables = super().get_css_variables()
        for token in TOKENS:
            variables.setdefault(token, BUILTIN[DEFAULT_THEME][token])
        return variables

    def token_color(self, token: str) -> str:
        """Hex colour of a token in the active theme, for Rich styles."""
        theme = self.available_themes.get(self.theme)
        value = theme.variables.get(token) if theme is not None else None
        return value or BUILTIN[DEFAULT_THEME][token]

    def paint(self, style: str) -> str:
        """Resolve token references in a Rich style string.

        Rich has no idea about CSS variables, so Rich-bound styles are resolved
        against the active theme here. Both ``$accent`` and a bare ``accent`` are
        accepted: CSS-adjacent call sites keep the ``$``, colour tables do not.
        """
        resolved = _TOKEN_REFERENCE.sub(lambda m: self.token_color(m.group(1)), style)
        return _BARE_TOKEN.sub(lambda m: self.token_color(m.group(0)), resolved)

    def _styled(self, text: str) -> Text:
        match = re.match(r"^\[([a-z-]+)\]", text)
        paint = self._TAG_TOKENS.get(match.group(1)) if match else None
        if paint is None:
            return Text(text)
        weight, token = paint
        style = f"{weight} {self.token_color(token)}".strip()
        return Text(text, style=style)

    def _write_line(self, text: str) -> None:
        journal = self.query_one("#journal", BlockJournal)
        journal.write(self._styled(text))

    def _write_dim(self, text: str, *, process: bool = True) -> None:
        """Dim gray status line.

        Grey process chatter (progress hints, graph notes, thinking) is what
        buries the answers during a long run, so inside a turn it goes into a
        buffer that closes as one collapsible line. Lines that summarise real
        work — a tool run, a block being revealed — pass process=False.
        """
        if process and self._chat_busy:
            self._process_block.append(text)
            self._thinking(text)
            return
        journal = self.query_one("#journal", BlockJournal)
        journal.write(Text(text, style=self.paint(self._DIM_STYLE)))

    def _thinking(self, text: str) -> None:
        """Grey chatter lands in the turn's single «thinking» block."""
        journal = self.query_one("#journal", BlockJournal)
        if self._thinking_block is None:
            self._thinking_block = journal.add_tool_block(
                Text(
                    "💭 " + self.translator.t("chat.thinking", count=0),
                    style=self.paint(self._DIM_STYLE),
                ),
                [],
                body_style=self.paint(self._DIM_STYLE),
                expanded=False,
            )
        block = self._thinking_block
        block.set_body(list(self._process_block), self.paint(self._DIM_STYLE))
        header = Text(
            "💭 " + self.translator.t("chat.thinking", count=len(self._process_block)),
            style=self.paint(self._DIM_STYLE),
        )
        block.set_title(header)

    def _close_process_block(self) -> None:
        """Fold the turn's thinking into the one block it belongs to."""
        self._close_thinking_block()

    def _close_thinking_block(self) -> None:
        if self._thinking_block is None:
            return
        block = self._thinking_block
        self._thinking_block = None
        if self._process_block:
            block.set_body(list(self._process_block), self.paint(self._DIM_STYLE))
        self._process_block.clear()

    def _brand_text(self) -> Text:
        """The side strip carries the name; the logo lives in the README."""
        return Text("Samurai AI", style=self.paint("bold accent"))

    def _write_user_message(self, text: str) -> None:
        journal = self.query_one("#journal", BlockJournal)
        journal.write(Text())
        journal.add_block(UserBlock(text))

    def _write_ai_message(self, response) -> None:  # type: ignore[no-untyped-def]
        journal = self.query_one("#journal", BlockJournal)
        header = (
            f"{response.model_id} · {response.latency_ms}ms · "
            f"{response.usage.prompt_tokens}+{response.usage.completion_tokens} tok"
        )
        journal.write(Text())
        journal.write(Text(header, style=self.paint(self._DIM_STYLE)))
        journal.write(Markdown(response.text))
        self._run_tokens += response.usage.prompt_tokens + response.usage.completion_tokens
        self._last_ai_text = response.text
        self._last_ai_code = _first_code_block(response.text)

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield ViewTabs(
                Tab(self.translator.t("tabs.chat"), id="tab-chat"),
                Tab(self.translator.t("tabs.ops"), id="tab-ops"),
                Tab(self.translator.t("tabs.bounty"), id="tab-bounty"),
                Tab(self.translator.t("tabs.hunt"), id="tab-hunt"),
                id="view-tabs",
                active="",
            )
            yield PlusButton("+", id="new-tab")
            yield Static(id="topbar-space")
            yield Static(id="brand")
        yield Static(id="attack-map")
        yield Horizontal(id="operations-strip")
        yield Static(id="killchain")
        yield Static(id="welcome")
        with Horizontal(id="body"):
            with ContentSwitcher(initial="journal", id="views"):
                yield BlockJournal(id="journal")
                yield BlockJournal(id="view-ops")
                yield BlockJournal(id="view-bounty")
                yield BlockJournal(id="view-hunt")
            with Vertical(id="side"):
                yield Static(id="side-workspace")
                yield Static(id="side-swarm")
                yield Static(id="side-access")
                yield Static(id="side-creds")
        with Horizontal(id="ops-strip"):
            yield Static(id="strip-agents", classes="strip-item")
            yield Static(id="strip-todo", classes="strip-item")
            yield Static(id="strip-task", classes="strip-item")
            yield Static(id="strip-goal", classes="strip-item")
        yield Static(id="thinking")
        yield OptionList(id="command-suggestions")
        yield HistoryInput(placeholder=self.translator.t("input.placeholder"), id="command-input")
        yield Static(id="hint-bar")
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        self._thread_id = threading.get_ident()
        self._apply_mouse()
        # a finished drag copies itself — the operator should never think
        # about *how* to copy; like Claude Code, selection IS the copy
        self.watch(self.screen, "selections", self._on_screen_selections, init=False)
        # the model may ask a question at any point of a run
        if getattr(self.orchestrator, "question_notice", None) is None:
            self.orchestrator.question_notice = self.question_notice_from_thread
        try:
            if self.requested_operation_id:
                self.operation = self.service.get(self.requested_operation_id)
            else:
                self.operation = self.service.latest()
        except OperationNotFoundError:
            # Fresh install or empty base: the first chat message will spawn
            # its own operation tab, so there is nothing to load yet.
            self.operation = None

        self._refresh_chrome()
        self._show_project_picker()
        for problem in self._theme_problems:
            self._write_dim(self.translator.t("theme.problem", problem=problem))
        for problem in self._key_problems:
            self._write_dim(self.translator.t("keys.problem", problem=problem))
        if self._locale_known:
            self._after_locale()
        else:
            self.push_screen(LanguagePromptScreen(self.translator), self._on_locale_chosen)

    def _on_locale_chosen(self, locale: str | None) -> None:
        if locale:
            self.settings.set_locale(locale)
            self.translator.set_locale(locale)
            self._locale_known = True
            self._retitle_tabs()
        self.query_one("#command-input", Input).placeholder = self.translator.t("input.placeholder")
        self._after_locale()

    def _after_locale(self) -> None:
        self._write_welcome()
        if not self._chat_history:
            self._restore_chat()
        self._refresh_chrome()
        # the search counters move slowly; a three-minute beat is plenty and
        # costs one render of a hidden pane at most
        self.set_interval(HUNT_REFRESH_SECONDS, self.refresh_hunt)
        self.set_interval(1.0, self._status_tick)
        self.focus_command_input(force=True)
        self._maybe_prompt_trust()

    def _retitle_tabs(self) -> None:
        tabs = self.query_one("#view-tabs", Tabs)
        for tab_id, key in (
            ("tab-ops", "tabs.ops"),
            ("tab-bounty", "tabs.bounty"),
            ("tab-hunt", "tabs.hunt"),
        ):
            tab = tabs.query_one(f"#{tab_id}", Tab)
            tab.label = self.translator.t(key)

    def _chat_label(self) -> str:
        """The chat tab is named after the operation; without one it is a plain journal."""
        return self._auto_title or (self.operation.name if self.operation else "") or (
            self.translator.t("tabs.chat")
        )

    def _write_welcome(self) -> None:
        journal = self.query_one("#journal", BlockJournal)
        journal.write(self._banner())
        self._write_dim(self.translator.t("welcome.help-hint"))

    def _banner(self) -> Text:
        """Opening frame: name and version, nothing else."""
        block = Text()
        block.append("Samurai AI", style=self.paint("bold accent"))
        block.append(f"  v{__version__}", style=self.paint("dim text-faint"))
        return block

    def on_click(self, event: events.Click) -> None:
        """A click on the journal opens the newest collapsed block."""
        widget: Any = event.widget
        while widget is not None:
            if getattr(widget, "id", None) == "journal":
                return
            widget = widget.parent

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-input":
            return
        popup = self._suggestions_popup()
        if popup.display and popup.highlighted is not None:
            highlighted = str(popup.get_option_at_index(popup.highlighted).id or "")
            if highlighted and highlighted != event.value.strip():
                self.accept_suggestion()
                return
        popup.display = False
        command = self._attach_paths(self._expand_pastes(event.value.strip()))
        event.input.value = ""
        if not command and self._collapsed:
            # Enter on an empty line opens the newest collapsed block
            self._expand_outputs()
            return
        if command:
            if not self._history or self._history[-1] != command:
                self._history.append(command)
                self._history = self._history[-200:]
                self.settings.set_value("tui.history", json.dumps(self._history))
            self._history_index = None
            self._history_draft = ""
            self._handle_command(command)

    def on_paste(self, event: events.Paste) -> None:
        """A big paste becomes a marker in the field; the text expands on send.

        Markers alone are a known trap (a harness that sends the marker instead
        of the content loses the paste), so `_expand_pastes` puts it back.
        """
        text = event.text
        field = self.focused
        if not isinstance(field, Input) or field.id != "command-input":
            return
        if len(text.splitlines()) < 8 and len(text) < 1200:
            return  # a short paste types itself in
        event.prevent_default()
        event.stop()
        index = len(self._pastes) + 1
        self._pastes[index] = text
        marker = self.translator.t(
            "input.paste-marker", index=index, lines=len(text.splitlines())
        )
        position = field.cursor_position
        field.value = field.value[:position] + marker + field.value[position:]
        field.cursor_position = position + len(marker)
        self._write_dim(
            self.translator.t("input.paste-stored", index=index, chars=len(text)),
            process=False,
        )

    def _expand_pastes(self, text: str) -> str:
        """Put every `[pasted #N]` marker back to its content; markers then clear."""
        if not self._pastes:
            return text
        for index, block in list(self._pastes.items()):
            marker = self.translator.t(
                "input.paste-marker", index=index, lines=len(block.splitlines())
            )
            if marker in text:
                text = text.replace(marker, block)
        self._pastes.clear()
        return text

    def _attach_paths(self, text: str) -> str:
        """`@path` mentions become fenced file contents; bad paths stay as text."""
        if "@" not in text:
            return text
        import re

        def replace(match: re.Match[str]) -> str:
            path = Path(match.group(1))
            if not path.is_file():
                self._write_dim(self.translator.t("input.attach-missing", path=str(path)))
                return match.group(0)
            try:
                body = path.read_text(errors="replace")[: self._ATTACH_LIMIT]
            except OSError as exc:
                self._write_dim(f"[attach] {exc}")
                return match.group(0)
            self._write_dim(
                self.translator.t("input.attached", path=str(path), chars=len(body)),
                process=False,
            )
            return f"\n\n`{path}`:\n```\n{body}\n```\n"

        return re.sub(r"@([\w./\-]+\.[\w]+)", replace, text)

    def history_step(self, direction: int) -> None:
        if not self._history:
            return
        command_input = self.query_one("#command-input", Input)
        if direction < 0:
            if self._history_index is None:
                self._history_draft = command_input.value
                self._history_index = len(self._history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            value = self._history[self._history_index]
        else:
            if self._history_index is None:
                return
            if self._history_index < len(self._history) - 1:
                self._history_index += 1
                value = self._history[self._history_index]
            else:
                self._history_index = None
                value = self._history_draft
        command_input.value = value
        command_input.cursor_position = len(value)

    def focus_command_input(self, *, force: bool = False) -> None:
        """Return focus to the command input unless the operator is busy.

        A turn finishing or a background refresh must not take focus away while
        the operator drags a selection or scrolls a list — that is the bug half
        the terminal agents get filed for. Explicit actions pass ``force``.
        """
        if not force:
            if self.screen.selections:
                return
            if isinstance(self.focused, (OptionList, Tabs)):
                return
        self.query_one("#command-input", Input).focus()

    def _on_screen_selections(self) -> None:
        """Selection changes during the drag; copy once it settles."""
        timer = self._selection_copy_timer
        if timer is not None:
            timer.stop()
        self._selection_copy_timer = self.set_timer(0.35, self._copy_settled_selection)

    def _copy_settled_selection(self) -> None:
        self._selection_copy_timer = None
        text = self._selected_text()
        if not text or text == self._last_auto_copy:
            return
        self._last_auto_copy = text
        self.copy_to_clipboard(text)

    def _selected_text(self) -> str | None:
        """What is selected, the focused input's selection included.

        Text selected inside the command input is not a screen selection, so
        without this Ctrl+C there armed the quit instead of copying.
        """
        focused = self.focused
        if isinstance(focused, Input):
            selection = getattr(focused, "selection", None)
            if selection is not None and selection.start != selection.end:
                start, end = sorted((selection.start, selection.end))
                return focused.value[start:end]
        screen = self.screen
        getter = getattr(screen, "get_selected_text", None)
        text = getter() if callable(getter) else None
        return text or None

    def action_copy_selection(self) -> None:
        """Ctrl+Shift+C: copy the selection, always, in every screen."""
        text = self._selected_text()
        if text is None:
            self._write_dim(self.translator.t("mouse.nothing-selected"))
            return
        self.copy_to_clipboard(text)
        self._write_dim(self.translator.t("mouse.copied", chars=len(text)))

    def action_quit_requested(self) -> None:
        """First press stops the work, second press leaves.

        Half of a long run is spent waiting for a model or a container, and the
        only way out used to be killing the terminal. The stop is cooperative —
        the step in flight finishes, the next one is not started — so nothing is
        left half-done; interrupting again is how you leave anyway.
        """
        # a selection is what the operator wants to copy; quitting is the rare
        # case once something is highlighted
        selected = self._selected_text()
        if selected is not None:
            self.copy_to_clipboard(selected)
            self._write_dim(self.translator.t("mouse.copied", chars=len(selected)))
            return
        now = time.monotonic()
        if now - self._last_quit_request <= self._QUIT_CONFIRM_SECONDS:
            self.exit()
            return
        self._last_quit_request = now
        if self._stop_running_work():
            self._write_line("[info] " + self.translator.t("app.stop-hint"))
            return
        self._write_line("[info] " + self.translator.t("app.quit-hint"))

    def _stop_running_work(self) -> bool:
        """Ask whatever is running to stand down; True when something was."""
        stopped = False
        if getattr(self, "_chat_busy", False) and not self._stop_requested:
            self.request_stop()
            stopped = True
        if self.stop_subagent():
            self._write_dim(self.translator.t("sessions.stopping", run="-"))
            stopped = True
        return stopped

    def action_view_journal(self) -> None:
        self._switch_view("journal")

    def action_view_ops(self) -> None:
        self._switch_view("view-ops")

    def action_view_bounty(self) -> None:
        self._switch_view("view-bounty")

    def action_view_tasks(self) -> None:
        self._switch_view("view-hunt")

    def action_view_next(self) -> None:
        self._cycle_view(1)

    def action_view_previous(self) -> None:
        self._cycle_view(-1)

    def _cycle_view(self, step: int) -> None:
        panes = [pane for pane, tab in (
            ("journal", "tab-chat"), ("view-ops", "tab-ops"),
            ("view-bounty", "tab-bounty"), ("view-hunt", "tab-hunt"),
        ) if pane == "journal" or self.query_one(f"#{tab}", Tab).display]
        current = self.query_one("#views", ContentSwitcher).current or "journal"
        index = panes.index(current) if current in panes else 0
        self._switch_view(panes[(index + step) % len(panes)])

    def action_toggle_plan(self) -> None:
        self.run_worker(self._toggle_plan(), exclusive=False)

    async def _toggle_plan(self) -> None:
        if self._plan_mode:
            # Leaving plan mode always costs a confirmation, in every mode.
            allowed = await self.push_screen_wait(
                AccessPromptScreen(
                    self.translator,
                    self.translator.t("plan.leave-action"),
                    "r2",
                )
            )
            if not allowed:
                return
        self._set_plan_mode(not self._plan_mode)

    def _set_plan_mode(self, enabled: bool) -> None:
        self._plan_mode = enabled
        self._write_line("[status] " + self.translator.t("plan.on" if enabled else "plan.off"))
        operation = self.operation
        if operation is not None:
            self.service.repository.append_event(
                OperationEvent(
                    operation_id=operation.id,
                    event_type="operator.plan_mode",
                    payload={"enabled": enabled},
                )
            )
        self._refresh_chrome()
        self.query_one("#command-input", Input).focus()

    def action_toggle_journal(self) -> None:
        views = self.query_one("#views", ContentSwitcher)
        views.display = not views.display
        self.focus_command_input(force=True)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        # OSC52 does not reach the clipboard on every terminal (e.g. macOS
        # Terminal.app), so also go through the native clipboard tool.
        for command in (
            ("pbcopy",),
            ("wl-copy",),
            ("xclip", "-selection", "clipboard"),
            ("xsel", "--clipboard", "--input"),
            ("clip",),
        ):
            if shutil.which(command[0]):
                try:
                    subprocess.run(command, input=text.encode("utf-8"), check=False, timeout=2)
                except OSError:
                    pass
                break

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        # The Tabs widget fires one TabActivated for the first tab while
        # mounting; that one must not switch away from the chat journal.
        self._tab_activations_seen += 1
        if self._tab_activations_seen == 1:
            # Detach the mount-time activation: no dashboard tab is active
            # while the chat journal is on screen, and the first real click
            # on "Red Team OPS" must still fire.
            event.tabs.active = ""
            return
        pane = self._VIEW_BY_TAB.get(str(event.tab.id))
        if pane is None:
            return
        self._switch_view(pane)


    def _store_tab_state(self) -> None:
        """Remember the visible operation's view state before switching."""
        if self.operation is None:
            return
        self._tab_states[self.operation.id] = {
            "auto_title": self._auto_title,
            "chat_history": list(self._chat_history),
            "last_ai_text": self._last_ai_text,
            "last_ai_code": self._last_ai_code,
            "last_user_text": self._last_user_text,
            "turn_todo": list(self._turn_todo),
            "proposed_plan": self._proposed_plan,
            "killchain_phase": self._killchain_phase,
        }

    def _restore_tab_state(self) -> None:
        if self.operation is None:
            return
        state = self._tab_states.get(self.operation.id)
        if state is None:
            return
        self._auto_title = state["auto_title"]
        self._chat_history = state["chat_history"]
        self._last_ai_text = state["last_ai_text"]
        self._last_ai_code = state["last_ai_code"]
        self._last_user_text = state["last_user_text"]
        self._turn_todo = state["turn_todo"]
        self._proposed_plan = state["proposed_plan"]
        self._killchain_phase = state["killchain_phase"]
        self._restore_chat()

    def _switch_view(self, pane: str) -> None:
        tab = self._TAB_BY_VIEW.get(pane)
        if tab is not None and self.query_one("#view-tabs", Tabs).active != tab:
            self.query_one("#view-tabs", Tabs).active = tab
        self.query_one("#views", ContentSwitcher).current = pane
        if pane != "journal":
            self._render_view(pane)
        self.focus_command_input(force=True)

    def _cmd_new_operation(self) -> None:
        self._show_new_operation()

    def action_new_operation(self) -> None:
        self._show_new_operation()

    def _apply_proposed_plan(self) -> None:
        """Turn the model's proposal into the operation's plan and approve it."""
        from samurai_ai.domain.kernel import PlanTask, RiskClass

        if self.operation is None:
            self._write_line("[error] " + self.translator.t("status.no-operation"))
            return
        proposal = getattr(self, "_proposed_plan", None)
        if not proposal:
            self._write_dim(self.translator.t("plan.no-proposal"))
            return
        title, entries = proposal
        tasks = []
        for index, entry in enumerate(entries, start=1):
            risk = RiskClass.R1
            if entry.get("risk_class"):
                try:
                    risk = RiskClass(str(entry["risk_class"]).lower())
                except ValueError:
                    pass
            tasks.append(
                PlanTask(
                    id=f"t{index}",
                    title=str(entry["title"]),
                    risk_class=risk,
                    depends_on=tuple(
                        str(dep) for dep in entry.get("depends_on", []) if str(dep)
                    ),
                )
            )
        current = self.kernel.get_current_plan(self.operation.id)
        if current is None:
            self.kernel.create_plan(self.operation.id, title, tasks, [])
        else:
            self.kernel.revise_plan(self.operation.id, title, tasks, current.gates)
        self.kernel.approve_plan(self.operation.id)
        self._proposed_plan = None
        self._write_dim(self.translator.t("plan.applied", title=title, count=len(tasks)))
        self._refresh_chrome()
        self._show_task_board()

    def _discard_proposed_plan(self) -> None:
        if not getattr(self, "_proposed_plan", None):
            self._write_dim(self.translator.t("plan.no-proposal"))
            return
        self._proposed_plan = None
        self._write_dim(self.translator.t("plan.discarded"))
        self._refresh_chrome()


    def action_toggle_follow(self) -> None:
        """Ctrl+T: tail-follow on or off — read back without losing the stream."""
        journal = self.query_one("#journal", BlockJournal)
        follow = journal.toggle_follow()
        self._write_dim(self.translator.t("journal.follow-on" if follow else "journal.follow-off"))


    def action_journal_search(self) -> None:
        """`/` opens the journal search; n/N walk the matches."""
        field = self.query_one("#command-input", Input)
        field.value = "/find "
        field.focus()
        field.cursor_position = len(field.value)

    def action_journal_next(self) -> None:
        self._walk_journal(1)

    def action_journal_previous(self) -> None:
        self._walk_journal(-1)

    def _walk_journal(self, step: int) -> None:
        term = getattr(self, "_journal_term", "")
        if not term:
            return
        journal = self.query_one("#journal", BlockJournal)
        matches = journal.search(term)
        if not matches:
            self._write_dim(self.translator.t("journal.search-none", term=term))
            return
        current = getattr(self, "_journal_match", -1)
        self._journal_match = (current + step) % len(matches)
        journal.scroll_to(y=matches[self._journal_match], animate=False)
        self._write_dim(
            self.translator.t(
                "journal.search-hit",
                index=self._journal_match + 1,
                total=len(matches),
            )
        )

    def action_op_next(self) -> None:
        self._cycle_operation(1)

    def action_op_previous(self) -> None:
        self._cycle_operation(-1)

    def _cycle_operation(self, step: int) -> None:
        if len(self._open_tabs) < 2:
            return
        current = self.operation.id if self.operation else ""
        index = self._open_tabs.index(current) if current in self._open_tabs else 0
        self._open_operation(self._open_tabs[(index + step) % len(self._open_tabs)])

    def _open_operation(self, operation_id: str) -> None:
        """Switch the visible tab to an open operation without losing state."""
        if self.operation is not None and self.operation.id == operation_id:
            return
        try:
            operation = self.service.get(operation_id)
        except OperationNotFoundError:
            self._open_tabs = [op for op in self._open_tabs if op != operation_id]
            self._refresh_operations_strip()
            return
        self._store_tab_state()
        self.operation = operation
        self._auto_title = None
        self._graph_used = False
        self._last_ai_text = None
        self._last_ai_code = None
        self._last_user_text = ""
        self._turn_todo = []
        self._killchain_phase = ""
        self._chat_history.clear()
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._restore_chat()
        self._restore_tab_state()
        self._write_dim(self.translator.t("resume.done", name=operation.name), process=False)
        self._switch_view("journal")
        self._refresh_chrome()

    def _close_operation(self) -> None:
        """Close the current tab; the operation itself stays in the store."""
        if self.operation is None:
            return
        self._store_tab_state()
        closed = self.operation.id
        self._open_tabs = [op for op in self._open_tabs if op != closed]
        if self._open_tabs:
            self._open_operation(self._open_tabs[-1])
            return
        self.operation = None
        self._auto_title = None
        self._graph_used = False
        self._chat_history.clear()
        self._last_ai_text = None
        self._last_ai_code = None
        self._last_user_text = ""
        self._turn_todo = []
        self._killchain_phase = ""
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._switch_view("journal")
        self._refresh_chrome()

    def _show_replay(self) -> None:
        """Scrub the operation's event journal in a side panel."""
        if self.operation is None:
            self._write_line("[replay] " + self.translator.t("status.no-operation"))
            return
        from samurai_ai.tui.replay import replay_lines

        events = self.service.events(self.operation.id)
        lines = replay_lines(events)
        journal = self.query_one("#journal", BlockJournal)
        journal.write(Text())
        journal.write(Text("── replay ──", style=self.paint("dim accent")))
        for line in lines[-40:]:
            journal.write(Text(line, style=self.paint("dim text-dim")))

    def _show_resume(self) -> None:
        """Pick an earlier operation and carry on where it stopped."""
        from samurai_ai.tui.screens import PickerScreen

        current = self.operation.id if self.operation else ""
        operations = [op for op in self.service.list_all() if op.id != current]
        picks = [
            (op.id, f"{op.id}  {op.name}", f"{op.mode.value} · {op.status.value}")
            for op in operations[:20]
        ]
        self.push_screen(
            PickerScreen(self.translator, "resume.title", "resume.hint", "resume.empty", picks),
            self._on_resume,
        )

    def _on_resume(self, operation_id: str | None) -> None:
        if operation_id is None:
            self.focus_command_input(force=True)
            return
        try:
            operation = self.service.get(operation_id)
        except OperationNotFoundError:
            self._write_line("[error] " + self.translator.t("status.no-operation"))
            return
        self.operation = operation
        self._auto_title = None
        self._graph_used = False
        self._chat_history.clear()
        self._last_ai_text = None
        self._last_ai_code = None
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._restore_chat()
        self._write_dim(self.translator.t("resume.done", name=operation.name), process=False)
        self._switch_view("journal")
        self._refresh_chrome()

    def _show_new_operation(self) -> None:
        self.push_screen(NewOperationScreen(self.translator), self._on_new_operation)

    def _on_new_operation(self, choice: tuple[str, str, str] | None) -> None:
        if choice is None:
            self.focus_command_input(force=True)
            return
        name, mode, scope = choice
        includes = tuple(rule.strip() for rule in scope.split(",") if rule.strip()) or (
            "127.0.0.1/32",
        )
        operation = self.service.create(name, OperationMode(mode), ScopeContract(includes=includes))
        self.operation = operation
        self._auto_title = None
        self._graph_used = False
        self._chat_history.clear()
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._write_dim(self.translator.t("newop.created", name=name, mode=mode))
        self._switch_view("journal")
        self._refresh_chrome()

    def _render_view(self, pane: str) -> None:
        log = self.query_one(f"#{pane}", BlockJournal)
        log.set_lines(
            [
                line if isinstance(line, Text) else self._styled(line)
                for line in self._view_lines(pane)
            ]
        )

    def _view_lines(self, pane: str) -> list[str | Text]:
        if pane == "view-ops":
            return self._ops_lines()
        if pane == "view-bounty":
            return self._bounty_lines()
        if pane == "view-hunt":
            return self._hunt_lines()
        return []

    _TASK_COLORS = {
        "done": "success",
        "skipped": "text-dim",
        "in-progress": "warning",
        "blocked": "danger",
    }

    _STEP_COLORS = {
        "executed": "success",
        "skipped": "text-dim",
        "executing": "warning",
        "blocked": "danger",
    }

    def _ops_lines(self) -> list[str | Text]:
        if not self.operation:
            return [
                Text(self.translator.t("status.no-operation"), style=self.paint(self._DIM_STYLE))
            ]
        lines: list[str | Text] = [
            Text(self.translator.t("ops.todo"), style=self.paint("bold $accent"))
        ]
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.tasks:
            lines.append(Text(self.translator.t("ops.no-tasks"), style=self.paint(self._DIM_STYLE)))
        else:
            for task in plan.tasks:
                color = self.paint(
                    "$" + self._TASK_COLORS.get(task.status.value, "text-faint")
                )
                lines.append(
                    Text.assemble(
                        ("■ ", color),
                        entity_span(
                            task.id, "task", task.id, base_style=self.paint("bold $text")
                        ),
                        (f" [{task.status.value}] ", color),
                        (task.title, self.paint("$text-dim")),
                    )
                )
        if plan is not None and plan.gates:
            lines.append(Text(self.translator.t("ops.gates"), style=self.paint("bold $accent")))
            for gate in plan.gates:
                color = self.paint(
                    "$success" if gate.status.value == "passed" else "$text-faint"
                )
                lines.append(
                    Text.assemble(
                        ("■ ", color),
                        (f"{gate.id} ", self.paint("bold $text")),
                        (f"[{gate.status.value}] ", color),
                        (gate.name, self.paint("$text-dim")),
                    )
                )
        steps = self.redteam.list_steps(self.operation.id)
        lines.append(Text(self.translator.t("ops.heatmap"), style=self.paint("bold $accent-alt")))
        if not steps:
            lines.append(Text(self.translator.t("ops.no-steps"), style=self.paint(self._DIM_STYLE)))
        else:
            row: list[tuple[str, str]] = []
            for step in steps:
                color = self._STEP_COLORS.get(step.status.value, "border")
                row.append((f" {step.technique_id} ", self.paint(f"text on {color}")))
                if len(row) == 6:
                    lines.append(Text.assemble(*row))
                    row = []
            if row:
                lines.append(Text.assemble(*row))
        return lines

    def _bounty_lines(self) -> list[str | Text]:
        if not self.operation:
            return [
                Text(self.translator.t("status.no-operation"), style=self.paint(self._DIM_STYLE))
            ]
        scope = self.operation.scope
        lines: list[str | Text] = [
            Text(self.translator.t("bounty.scope"), style=self.paint("bold $risk-low"))
        ]
        lines.append(
            Text(
                self.translator.t("scope.includes", rules=", ".join(scope.includes)),
                style=self.paint("$risk-low"),
            )
        )
        excludes = ", ".join(scope.excludes) or self.translator.t("scope.none")
        lines.append(
            Text(self.translator.t("scope.excludes", rules=excludes), style=self.paint("$risk-mid"))
        )
        assets = self.playbooks.list_assets(self.operation.id)
        lines.append(
            Text(
                self.translator.t("bounty.assets", count=len(assets)),
                style=self.paint("bold $risk-low"),
            )
        )
        for entry in assets[:10]:
            lines.append(
                Text(
                    f"  {entry.kind.value} | {entry.value} | {entry.source}",
                    style=self.paint("$text-dim"),
                )
            )
        lines.append(
            Text(self.translator.t("bounty.findings"), style=self.paint("bold $accent-alt"))
        )
        return lines + self._finding_lines()

    # --- slash-command suggestions -------------------------------------

    def _command_specs(self) -> list[tuple[str, str]]:
        specs: list[tuple[str, str]] = []
        for line in self.translator.help_lines():
            if not line.startswith("/"):
                continue
            name, _, description = line.partition(" ")
            specs.append((name, description.strip()))
        return specs

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "command-input":
            self._update_suggestions()

    def _update_suggestions(self) -> None:
        value = self.query_one("#command-input", Input).value
        popup = self.query_one("#command-suggestions", OptionList)
        matches: list[tuple[str, str]] = []
        if value.startswith("/") and " " not in value:
            matches = [
                (name, description)
                for name, description in self._command_specs()
                if name.startswith(value) and name != value
            ]
        if not matches:
            popup.display = False
            return
        popup.clear_options()
        popup.add_options(
            Option(f"{name}  {description}", id=name) for name, description in matches
        )
        popup.highlighted = 0
        popup.display = True

    def _suggestions_popup(self) -> OptionList:
        return cast("OptionList", self.query_one("#command-suggestions", OptionList))

    def suggestions_step(self, direction: int) -> bool:
        popup = self._suggestions_popup()
        if not popup.display or popup.option_count == 0:
            return False
        current = popup.highlighted or 0
        popup.highlighted = (current + direction) % popup.option_count
        return True

    def accept_suggestion(self) -> None:
        popup = self._suggestions_popup()
        if not popup.display or popup.highlighted is None:
            return
        option = popup.get_option_at_index(popup.highlighted)
        command_input = self.query_one("#command-input", Input)
        command_input.value = str(option.id or "")
        command_input.cursor_position = len(command_input.value)
        popup.display = False
        command_input.focus()

    def close_suggestions(self) -> None:
        popup = self._suggestions_popup()
        if popup.display:
            popup.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option is None:
            return
        if event.option_list.id != "command-suggestions":
            return
        command_input = self.query_one("#command-input", Input)
        command_input.value = str(event.option.id or "")
        command_input.cursor_position = len(command_input.value)
        self._suggestions_popup().display = False
        command_input.focus()

    # --- directory trust & yolo -----------------------------------------

    def _remember_project_operation(self, directory: str, operation_id: str) -> None:
        self.settings.set_value(f"tui.project.last.{directory}", operation_id)

    def _remember_project(self, directory: str) -> None:
        """Track a working directory as a project, newest last."""
        known = [p for p in self._projects if p != directory]
        self._projects = [*known, directory][-20:]
        self.settings.set_value("tui.projects", json.dumps(self._projects))

    def _known_projects(self) -> list[str]:
        raw = self.settings.get_value("tui.projects") or "[]"
        try:
            value = json.loads(raw)
        except ValueError:
            value = []
        listed = [str(item) for item in value] if isinstance(value, list) else []
        for directory in self._stored_trusted_dirs():
            if directory not in listed:
                listed.append(directory)
        return [d for d in listed if Path(d).is_dir()]

    def _show_project_picker(self) -> None:
        """Ask which project this session works in; a single one opens itself."""
        projects = self._known_projects()
        current = str(self._cwd)
        if len(projects) <= 1 and (not projects or projects[0] == current):
            self._remember_project(current)
            return
        labels = [
            (path, path + self.translator.t("project.current-marker") if path == current else path)
            for path in projects
        ]
        self.push_screen(
            ProjectPickerScreen(self.translator, labels), self._on_project_chosen
        )

    def _on_project_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            self._remember_project(str(self._cwd))
            return
        _, path = choice
        self._switch_project(path)

    def _switch_project(self, path: str) -> None:
        """Point the session at another directory and its last operation."""
        self._store_tab_state()
        self._cwd = Path(path)
        self._remember_project(path)
        self._saved_dir = path
        last = self.settings.get_value(f"tui.project.last.{path}") or ""
        if last:
            self._open_operation(last)
        self._write_dim(self.translator.t("project.switched", path=path))
        self._refresh_chrome()

    def _stored_trusted_dirs(self) -> list[str]:
        raw = self.settings.get_value("tui.trusted_dirs") or "[]"
        try:
            value = json.loads(raw)
        except ValueError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def _permission_mode(self) -> PermissionMode:
        stored = self.settings.get_value(_PERMISSION_MODE_KEY)
        if stored is None:
            # Older sessions only recorded the yolo flag.
            legacy = PermissionMode.YOLO if self._legacy_yolo else PermissionMode.ASK
            return legacy
        return parse_mode(stored, PermissionMode.ASK)

    @property
    def _legacy_yolo(self) -> bool:
        return self.settings.get_value(_YOLO_LEGACY_KEY) == "1"

    @property
    def _yolo(self) -> bool:
        """True when OS commands run without a prompt (yolo or never)."""
        return not self._permission_mode.prompts_for_tools

    def _set_permission_mode(self, mode: PermissionMode) -> None:
        previous = self._permission_mode
        self.settings.set_value(_PERMISSION_MODE_KEY, mode.value)
        # keep the legacy flag in step so older readers stay truthful
        self.settings.set_value(_YOLO_LEGACY_KEY, "0" if mode is PermissionMode.ASK else "1")
        self._write_line("[status] " + self.translator.t(f"permission.mode.{mode.value}"))
        self._record_permission_mode(previous, mode)
        if self._chat_history and (
            previous.allows_operator_questions != mode.allows_operator_questions
        ):
            # the prompt carries the autonomy hint, so it has to keep up
            self._chat_history[0] = Message(role="system", content=self._chat_system_prompt())
        self._refresh_chrome()

    def _record_permission_mode(self, previous: PermissionMode, mode: PermissionMode) -> None:
        """The mode change is operator state, so it belongs in the journal."""
        operation = self.operation
        if operation is None or previous is mode:
            return
        self.service.repository.append_event(
            OperationEvent(
                operation_id=operation.id,
                event_type="operator.permission_mode",
                payload={"from": previous.value, "to": mode.value},
            )
        )

    def _cycle_permission_mode(self) -> None:
        self._set_permission_mode(next_mode(self._permission_mode))

    def _handle_goal_command(self, argument: str) -> None:
        operation = self.operation
        if operation is None:
            self._write_line("[goal] " + self.translator.t("goal.no-operation"))
            return
        head, _, rest = argument.partition(" ")
        rest = rest.strip()
        try:
            match head:
                case "" | "status":
                    self._show_goal()
                case "pause":
                    self.goals.pause(self._current_goal_id())
                    self._show_goal()
                case "resume":
                    self.goals.resume(self._current_goal_id())
                    self._show_goal()
                case "cancel":
                    self.goals.cancel(self._current_goal_id())
                    self._show_goal()
                case "complete":
                    self.goals.complete(self._current_goal_id())
                    self._show_goal()
                case "next":
                    if not rest:
                        self._write_line("[goal] " + self.translator.t("goal.need-text"))
                        return
                    self.goals.create(operation.id, rest, queue=True)
                    self._show_goal()
                case _:
                    self.goals.create(operation.id, argument)
                    self._show_goal()
        except (GoalError, LookupError) as exc:
            self._write_line(f"[goal] {exc}")
        self._refresh_chrome()

    def _current_goal_id(self) -> str:
        goal = self._current_goal()
        if goal is None:
            raise LookupError(self.translator.t("goal.none"))
        return goal.id

    def _show_goal(self) -> None:
        goal = self._current_goal()
        if goal is None:
            self._write_line("[goal] " + self.translator.t("goal.none"))
        else:
            self._write_line(
                f"[goal] {goal.state.value} | {goal.objective} | "
                f"steps {goal.spent_steps}/{goal.budget_steps} "
                f"time {goal.spent_seconds}/{goal.budget_seconds}s"
                + (f" | {goal.block_reason}" if goal.block_reason else "")
            )
        if self.operation is None:
            return
        for queued in self.goals.list(self.operation.id):
            if queued.state.value == "queued":
                self._write_line(f"[goal] queued: {queued.objective}")

    _COMPACT_KEEP = 8
    _COMPACT_MAX_CHARS = 2000

    @staticmethod
    def _is_turn_payload(message: Message) -> bool:
        """True for messages the agent loop appended around an operator turn."""
        if message.role != "user":
            return True
        content = str(message.content)
        return content.startswith("[tool ") or content.startswith("[shell ")

    def _undo_last_turn(self) -> None:
        """Drop the last operator turn and everything the loop appended to it."""
        if len(self._chat_history) <= 1 or self._chat_busy:
            self._write_dim(self.translator.t("chat.undo-empty"))
            return
        dropped = 0
        while len(self._chat_history) > 1 and self._is_turn_payload(self._chat_history[-1]):
            self._chat_history.pop()
            dropped += 1
        if len(self._chat_history) > 1:
            self._chat_history.pop()
            dropped += 1
        self._expandable.clear()
        self._write_dim(self.translator.t("chat.undone"))
        if self.operation is not None:
            self.chat_history.drop_last(self.operation.id, dropped)
            self.service.repository.append_event(
                OperationEvent(
                    operation_id=self.operation.id,
                    event_type="chat.undo",
                    payload={"remaining": len(self._chat_history)},
                )
            )

    async def _compact_history(self) -> None:
        """Fold the oldest part of the conversation into one bounded digest."""
        if self._chat_busy:
            self._write_dim(self.translator.t("chat.compact-busy"))
            return
        kept = self._chat_history[-self._COMPACT_KEEP :]
        system = self._chat_history[:1]
        older = self._chat_history[1 : len(self._chat_history) - len(kept)]
        if not older:
            self._write_dim(self.translator.t("chat.compact-skip"))
            return
        model_id = self.models.default_model()
        if model_id is None:
            self._write_line("[error] " + self.translator.t("chat.no-model"))
            return
        digest = await self._summarize(older)
        if digest is None:
            return
        self._chat_history = [
            *system,
            Message(role="user", content=f"[compacted]\n{digest}"),
            *kept,
        ]
        self._write_dim(self.translator.t("chat.compacted", count=len(older), kept=len(kept)))
        if self.operation is not None:
            self.service.repository.append_event(
                OperationEvent(
                    operation_id=self.operation.id,
                    event_type="chat.compact",
                    payload={"compacted": len(older), "kept": len(kept)},
                )
            )

    async def _summarize(self, messages: list[Message]) -> str | None:
        model_id = self.models.default_model()
        if model_id is None:
            self._write_line("[error] " + self.translator.t("chat.no-model"))
            return None
        prompt = Message(
            role="user",
            content=self.translator.t("chat.compact-prompt"),
        )
        # the request helper sends whatever sits in the history, so point it at
        # the slice being folded while the summary call runs
        saved = self._chat_history
        self._chat_history = [*messages, prompt]
        reasoning = (
            self.settings.get_value(f"model.reasoning_effort.{model_id}")
            or self.models.default_reasoning_effort()
        )
        try:
            response = await self._request_chat_completion(
                self.operation.id if self.operation else "", model_id, reasoning, ()
            )
        finally:
            self._chat_history = saved
        if response is None:
            return None
        digest = response.text.strip()
        if not digest:
            self._write_dim(self.translator.t("chat.compact-skip"))
            return None
        return digest[: self._COMPACT_MAX_CHARS]

    def _drain_steering(self) -> None:
        """Fold injected messages into the turn before the next model request."""
        while self._steer_queue:
            steered = self._steer_queue.pop(0)
            self._chat_history.append(Message(role="user", content=steered))

    def _count_goal_step(self) -> None:
        """One model request against the active goal, enforcing the budget."""
        goal = self._current_goal()
        if goal is None or goal.state.value != "active":
            return
        blocked = self.goals.record_step(goal.id)
        if blocked.state.value == "blocked":
            self._write_line(
                "[goal] " + self.translator.t("goal.blocked", reason=blocked.block_reason)
            )
            self._refresh_chrome()

    def _tool_is_read_only(self, tool_id: str) -> bool:
        try:
            return self.execution.registry.get(tool_id).definition.read_only
        except Exception:  # noqa: BLE001 - unknown tool is not read-only
            return False

    def _reusable_approval(self, tool_id: str) -> bool:
        """A tool already approved for this operation needs no second prompt."""
        operation = self.operation
        if operation is None:
            return False
        action = f"tool.run {tool_id}"
        return any(
            approval.action == action and approval.task_id is None
            for approval in self.kernel.kernel.list_approvals(
                operation.id, status=ApprovalStatus.APPROVED
            )
        )

    def _directory_trusted(self) -> bool:
        if self._yolo:
            return True
        if self._dir_trusted is False:
            return False
        cwd = str(self._cwd)
        return cwd in self._session_trusted or cwd in self._stored_trusted_dirs()

    def _maybe_prompt_trust(self, force: bool = False) -> None:
        if not force and self._directory_trusted():
            self._dir_trusted = True
            return
        self.push_screen(
            TrustDirectoryScreen(self.translator, str(self._cwd)),
            self._on_trust_chosen,
        )

    def _on_trust_chosen(self, choice: str | None) -> None:
        cwd = str(self._cwd)
        if choice == "always":
            trusted = self._stored_trusted_dirs()
            if cwd not in trusted:
                trusted.append(cwd)
                self.settings.set_value("tui.trusted_dirs", json.dumps(trusted))
            self._session_trusted.add(cwd)
            self._dir_trusted = True
            self._write_line("[allow] " + self.translator.t("trust.granted", path=cwd))
        elif choice == "session":
            self._session_trusted.add(cwd)
            self._dir_trusted = True
            self._write_line("[allow] " + self.translator.t("trust.granted", path=cwd))
        else:
            self._dir_trusted = False
            self._write_line("[deny] " + self.translator.t("trust.denied", path=cwd))
        self._refresh_chrome()
        self.focus_command_input(force=True)

    def _toggle_yolo(self) -> None:
        target = (
            PermissionMode.ASK
            if self._permission_mode is PermissionMode.YOLO
            else PermissionMode.YOLO
        )
        self._set_permission_mode(target)

    def _apply_mouse(self) -> None:
        driver = self._driver
        if driver is None:
            return
        enable = getattr(driver, "_enable_mouse_support", None)
        disable = getattr(driver, "_disable_mouse_support", None)
        if enable is None or disable is None:
            return
        if self.settings.get_value("tui.mouse") == "0":
            disable()
        else:
            enable()

    def _toggle_mouse(self) -> None:
        enabled = self.settings.get_value("tui.mouse") == "0"
        self.settings.set_value("tui.mouse", "1" if enabled else "0")
        self._apply_mouse()
        self._write_dim(self.translator.t("mouse.enabled" if enabled else "mouse.disabled"))

    def _actor(self) -> str:
        """Who signs the action: the signed-in subject, or `operator` locally."""
        try:
            return str(self.access.current().label)
        except AccessError:
            return "operator"

    def _require_path(self, path: str) -> bool:
        """Gate a board action that is not a slash command."""
        try:
            self.access.require(path)
        except AccessError as exc:
            self._write_line(f"[access] {exc}")
            return False
        return True

    def _handle_login_command(self, argument: str) -> None:
        """`/login` prints the code, `/login configure <issuer> <client_id>` sets it up."""

        async def run() -> None:
            from samurai_ai.tui.screens import ArgumentScreen

            if argument.startswith("configure"):
                parts = argument.split()
                issuer = parts[1] if len(parts) > 1 else ""
                client = parts[2] if len(parts) > 2 else ""
                if not issuer or not client:
                    issuer = await self.push_screen_wait(
                        ArgumentScreen(self.translator, self.translator.t("auth.issuer"))
                    ) or ""
                    client = await self.push_screen_wait(
                        ArgumentScreen(self.translator, self.translator.t("auth.client"))
                    ) or ""
                try:
                    self.access.configure(issuer, client)
                except AccessError as exc:
                    self._write_line(f"[auth] {exc}")
                    return
                self._write_dim(self.translator.t("auth.configured", issuer=issuer))
                return
            try:
                code = self.access.begin()
            except AccessError as exc:
                self._write_line(f"[auth] {exc}")
                return
            self._write_line(
                self.translator.t("auth.code", code=code.user_code, url=code.verification_uri)
            )
            try:
                session = await asyncio.to_thread(self.access.complete, code)
            except AccessError as exc:
                self._write_line(f"[auth] {exc}")
                return
            self._write_dim(
                self.translator.t(
                    "auth.signed-in", who=session.principal.label, role=session.principal.role.value
                )
            )
            self._refresh_chrome()

        self.run_worker(run())

    def _handle_whoami_command(self, argument: str) -> None:
        try:
            principal = self.access.current()
        except AccessError as exc:
            self._write_line(f"[auth] {exc}")
            return
        self._write_line(
            self.translator.t("auth.whoami", who=principal.label, role=principal.role.value)
        )

    def _handle_logout_command(self, argument: str) -> None:
        ended = self.access.logout()
        self._write_dim(self.translator.t("auth.signed-out" if ended else "auth.no-session"))
        self._refresh_chrome()

    def _handle_team_command(self, argument: str) -> None:
        """`/team` lists; `add|rm|transfer|note` change."""
        if self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        parts = argument.split(maxsplit=3)
        action = parts[0] if parts else ""
        try:
            actor = self.access.current()
            if action == "add" and len(parts) >= 2:
                level = OperationLevel.VIEWER if len(parts) > 2 and parts[2] == "viewer" else (
                    OperationLevel.MEMBER
                )
                member = self.team.add_member(
                    self.operation.id, parts[1], level=level, actor=actor
                )
                self._write_dim(
                    self.translator.t(
                        "team.added", who=member.label, level=member.level.value
                    )
                )
                return
            if action == "rm" and len(parts) >= 2:
                self.team.remove_member(self.operation.id, parts[1], actor=actor)
                self._write_dim(self.translator.t("team.removed", who=parts[1]))
                return
            if action == "transfer" and len(parts) >= 2:
                self.team.transfer(self.operation.id, parts[1], actor=actor)
                self._write_dim(self.translator.t("team.transferred", who=parts[1]))
                return
            if action == "note" and len(parts) >= 4:
                note = self.team.note(
                    self.operation.id,
                    NoteTarget(parts[1]),
                    parts[2],
                    parts[3],
                    actor=actor,
                )
                self._write_dim(self.translator.t("team.noted", id=note.id))
                return
            self.team.assert_visible(self.operation.id, actor)
        except (AccessError, TeamworkError, ValueError) as exc:
            self._write_line(f"[team] {exc}")
            return
        owner = self.team.owner(self.operation.id)
        self._write_line(self.translator.t("team.owner", who=owner or "-"))
        for member in self.team.members(self.operation.id):
            self._write_line(f"[team] {member.label or member.subject} · {member.level.value}")

    def _handle_role_command(self, argument: str) -> None:
        parts = argument.split()
        try:
            if parts and parts[0] == "map" and len(parts) >= 3:
                self.access.map_role(parts[1], Role(parts[2]))
                self._write_dim(self.translator.t("auth.mapped", claim=parts[1], role=parts[2]))
                return
            if parts and parts[0] == "rm" and len(parts) >= 2:
                self.access.access.remove_mapping(parts[1])
                self._write_dim(self.translator.t("auth.unmapped", claim=parts[1]))
                return
        except (AccessError, ValueError) as exc:
            self._write_line(f"[role] {exc}")
            return
        mappings = self.access.access.list_mappings()
        if not mappings:
            self._write_dim(self.translator.t("auth.no-mappings"))
            return
        for claim, role in mappings:
            self._write_line(f"[role] {claim} -> {role.value}")

    def _handle_command(self, command: str) -> None:
        if command.startswith("/find "):
            term = command[6:].strip()
            self._journal_term = term
            self._journal_match = -1
            if term:
                self._walk_journal(1)
            return
        if command.startswith("!"):
            self._shell_mode(command[1:].strip())
            return
        name, _, argument = command.partition(" ")
        spec = find_command(name)
        if spec is None:
            if name.startswith("/"):
                self._write_line("[error] " + self.translator.t("error.unknown-command", name=name))
            else:
                self._start_chat(command)
            return
        self._run_command(spec, argument.strip())

    async def invoke_command(self, spec: CommandSpec) -> None:
        """Run a registry action from the palette, asking for its argument."""
        argument = ""
        if spec.takes_argument and spec.prompt_key:
            answer = await self.push_screen_wait(
                ArgumentScreen(self.translator, self.translator.t(spec.prompt_key))
            )
            if answer is None:
                return
            argument = answer
        self._run_command(spec, argument)

    def _run_command(self, spec: CommandSpec, argument: str) -> None:
        if spec.needs_operation and self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        if spec.name not in _OPEN_COMMANDS:
            try:
                self.access.require(command_path(spec))
            except AccessError as exc:
                self._write_line(f"[access] {exc}")
                return
        handler = getattr(self, spec.handler)
        handler(argument) if spec.takes_argument else handler()

    def action_inspect_last_run(self) -> None:
        if self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        runs = self.execution.list_runs(self.operation.id)
        if not runs:
            self._write_line(self.translator.t("inspector.no-runs"))
            return
        self._show_inspector(runs[-1].id)

    def _show_inspector(self, run_id: str) -> None:
        """Open the run inspector, or say which run is missing."""
        try:
            run = self.execution.get_run(run_id.strip())
        except Exception:  # noqa: BLE001 - unknown id is a message, not a crash
            self._write_line(self.translator.t("inspector.unknown-run", run=run_id))
            return
        adapter = self.execution.registry.get(run.tool_id, run.tool_version)
        definition = adapter.definition
        sections = self._inspector_sections(run, definition)
        command = self._run_command_text(run)
        actions: dict[str, object] = {}
        if command:
            actions["c"] = lambda: self._copy_text(
                command, self.translator.t("inspector.copy-command")
            )
            actions["r"] = lambda: self._start_shell_mode(command)
        self.push_screen(
            InspectorScreen(
                self.translator,
                self.translator.t(
                    "inspector.title",
                    run=run.id,
                    tool=definition.id,
                    version=definition.version,
                ),
                sections,
                actions,  # type: ignore[arg-type]
            )
        )

    # -- drill-down ----------------------------------------------------------

    def action_inspect(self, kind: str, ref: str) -> None:
        """A clicked entity link: an inline summary first, Enter goes deeper."""
        summary = self._inspect_summary(kind, ref)
        if summary is None:
            self._write_dim(self.translator.t("inspect.unknown", kind=kind, ref=ref))
            return
        title, lines = summary
        text = Text(title, style=self.paint("bold accent"))
        for line in lines:
            text.append(f"\n  {line}")
        text.append("\n")
        text.append_text(
            entity_span(
                self.translator.t("inspect.more"), kind, ref, base_style=self.paint("accent")
            )
        )
        # the "more" link jumps straight to the full screen

        self.query_one("#journal", BlockJournal).add_block(
            InspectSummaryBlock(text, kind, ref)
        )

    def action_inspect_full(self, kind: str, ref: str) -> None:
        self._inspect_full(kind, ref)

    def _inspect_summary(self, kind: str, ref: str) -> tuple[str, list[str]] | None:
        """Two or three lines about the entity, or None when it is unknown."""
        try:
            if kind == "run":
                run = self.execution.get_run(ref)
                return (
                    self.translator.t("inspect.run", run=run.id, tool=run.tool_id),
                    [
                        self.translator.t(
                            "inspect.run-line",
                            status=run.status.value,
                            code=run.exit_code,
                            when=run.created_at.strftime("%H:%M:%S"),
                        ),
                    ],
                )
            if kind == "task":
                task = self._find_task(ref)
                if task is None:
                    return None
                return (
                    f"{task.id} · {task.title}",
                    [
                        self.translator.t(
                            "inspect.task-line",
                            status=task.status.value,
                            risk=task.risk_class.value,
                            deps=len(task.depends_on),
                        )
                    ],
                )
            if kind == "finding":
                finding = self.kernel.get_finding_by_id(ref)
                return (
                    f"{finding.id} · {finding.title}",
                    [
                        self.translator.t(
                            "inspect.finding-line",
                            status=finding.status.value,
                            severity=finding.severity.value,
                        )
                    ],
                )
            if kind == "artifact":
                artifact = self.kernel.kernel.get_artifact(ref)
                if artifact is None:
                    return None
                return (
                    f"{artifact.id} · {artifact.kind.value}",
                    [
                        self.translator.t(
                            "inspect.artifact-line",
                            size=artifact.size_bytes,
                            summary=artifact.summary or "—",
                        )
                    ],
                )
            if kind == "agent":
                agent_run = self.agents.get(ref)
                if agent_run is None:
                    return None
                return (
                    f"{agent_run.id} · {agent_run.specialization.value}",
                    [
                        self.translator.t(
                            "inspect.agent-line",
                            status=agent_run.status.value,
                            task=agent_run.task_id,
                            depth=agent_run.depth,
                        )
                    ],
                )
            if kind == "approval":
                approval = self.kernel.kernel.get_approval(ref)
                if approval is None:
                    return None
                return (
                    f"{approval.id} · {approval.action}",
                    [
                        self.translator.t(
                            "inspect.approval-line",
                            status=approval.status.value,
                            risk=approval.risk_class.value,
                            by=approval.decided_by or "—",
                        )
                    ],
                )
        except Exception:  # noqa: BLE001 - a stale id is a note, not a crash
            return None
        return None

    def _find_task(self, ref: str) -> Any:
        """A task by id or exact title in the operation's current plan."""
        if self.operation is None:
            return None
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None:
            return None
        for task in plan.tasks:
            if task.id == ref or task.title == ref:
                return task
        return None

    def _inspect_full(self, kind: str, ref: str) -> None:
        """The full-screen detail behind an inline summary."""
        if kind == "run":
            self._show_inspector(ref)
            return
        if kind == "task":
            self._show_task_detail(ref)
            return
        if kind == "finding":
            self._show_finding_detail(ref)
            return
        sections = self._inspect_sections(kind, ref)
        if sections is None:
            self._write_dim(self.translator.t("inspect.unknown", kind=kind, ref=ref))
            return
        title, body = sections
        self.push_screen(InspectorScreen(self.translator, title, body, {}))

    def _inspect_sections(self, kind: str, ref: str) -> tuple[str, list[Section]] | None:
        if kind == "artifact":
            artifact = self.kernel.kernel.get_artifact(ref)
            if artifact is None:
                return None
            meta = [
                f"kind: {artifact.kind.value}",
                f"task: {artifact.task_id or '—'}",
                f"sha256: {artifact.sha256}",
                f"size: {artifact.size_bytes} B",
                f"by: {artifact.created_by}",
            ]
            # artifact paths are relative to the state dir's artifacts/ root
            root = self.kernel.kernel.artifact_root
            full = pathlib.Path(artifact.path)
            if not full.is_absolute():
                full = root.parent / full if (root.parent / full).exists() else root / full
            content = self._artifact_excerpt(str(full))
            return (
                f"{artifact.id} — {artifact.summary or artifact.kind.value}",
                [
                    (self.translator.t("inspect.meta"), meta),
                    (self.translator.t("inspect.content"), content),
                ],
            )
        if kind == "agent":
            run = self.agents.get(ref)
            if run is None:
                return None
            lines = [
                f"status: {run.status.value}",
                f"task: {run.task_id}",
                f"specialization: {run.specialization.value}",
                f"depth: {run.depth}",
                f"started: {run.created_at:%Y-%m-%d %H:%M:%S}",
                (
                    f"finished: {run.finished_at:%Y-%m-%d %H:%M:%S}"
                    if run.finished_at
                    else "finished: —"
                ),
            ]
            return (f"{run.id}", [(self.translator.t("inspect.meta"), lines)])
        if kind == "approval":
            approval = self.kernel.kernel.get_approval(ref)
            if approval is None:
                return None
            lines = [
                f"action: {approval.action}",
                f"risk: {approval.risk_class.value}",
                f"status: {approval.status.value}",
                f"reason: {approval.reason or '—'}",
                f"requested by: {approval.requested_by}",
                f"decided by: {approval.decided_by or '—'}",
            ]
            return (f"{approval.id}", [(self.translator.t("inspect.meta"), lines)])
        return None

    _ARTIFACT_EXCERPT_LINES = 200

    def _artifact_excerpt(self, path: str) -> list[str]:
        """Bounded text of an artifact; unreadable or binary says so."""
        try:
            raw = pathlib.Path(path).read_bytes()
        except OSError as exc:
            return [self.translator.t("inspect.unreadable", error=exc)]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return [self.translator.t("inspect.binary", size=len(raw))]
        lines = text.splitlines()
        if len(lines) > self._ARTIFACT_EXCERPT_LINES:
            head = lines[: self._ARTIFACT_EXCERPT_LINES]
            head.append(
                self.translator.t(
                    "inspect.truncated", left=len(lines) - self._ARTIFACT_EXCERPT_LINES
                )
            )
            return head
        return lines or [self.translator.t("inspect.empty")]

    # -- subagent sessions ---------------------------------------------------
    def _cmd_subagent(self, argument: str) -> None:
        self.run_worker(self.spawn_subagent(argument), exclusive=False)

    def action_open_sessions(self) -> None:
        self._show_sessions()

    def action_attach_session(self) -> None:
        """Follow the daemon's stream for this operation from inside the TUI."""
        if self.operation is None:
            self._write_line("[attach] " + self.translator.t("status.no-operation"))
            return
        socket_path = serve_socket_path()
        if not socket_path.exists():
            self._write_line("[attach] " + self.translator.t("attach.no-daemon"))
            return
        self.push_screen(AttachScreen(self.translator, socket_path, self.operation.id))

    def action_open_task_board(self) -> None:
        self._show_task_board()

    def action_open_finding_board(self) -> None:
        self._show_finding_board()

    def _session_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for run in self.child_runs():
            rows.append(
                (
                    run.id,
                    self.translator.t(
                        "sessions.row",
                        run=run.id,
                        status=run.status.value,
                        task=run.task_id,
                        depth=run.depth,
                    ),
                )
            )
        return rows

    def _show_sessions(self) -> None:
        if self.operation is None:
            self._write_line("[sessions] " + self.translator.t("status.no-operation"))
            return
        self.push_screen(
            SessionsScreen(self.translator, self._session_rows(), self._stop_session),
            lambda outcome: self._show_session_detail(outcome) if outcome else None,
        )

    def _stop_session(self, run_id: str) -> None:
        if not self.stop_subagent(run_id):
            self._write_line(self.translator.t("sessions.not-running", run=run_id))
            return
        self._write_dim(self.translator.t("sessions.stopping", run=run_id))

    def _show_session_detail(self, run_id: str) -> None:
        run = next((item for item in self.child_runs() if item.id == run_id), None)
        if run is None:
            self._write_line(self.translator.t("sessions.unknown", run=run_id))
            return
        self.push_screen(
            SessionDetailScreen(
                self.translator,
                self.translator.t("sessions.detail-title", run=run.id, task=run.task_id),
                self._session_sections(run),
            ),
            lambda outcome: self._on_session_detail(outcome, run.id, run.task_id),
        )

    def _on_session_detail(self, outcome: str | None, run_id: str, task_id: str) -> None:
        if outcome == "stop":
            self._stop_session(run_id)
        elif outcome == "replay":
            self._replay_task(task_id)
        elif outcome == "inspect":
            self._inspect_last_child_run(run_id)

    def _inspect_last_child_run(self, run_id: str) -> None:
        run = next((item for item in self.child_runs() if item.id == run_id), None)
        if run is None:
            self._write_line(self.translator.t("sessions.unknown", run=run_id))
            return
        history = self._child_tool_runs(run)
        if not history:
            self._write_line(self.translator.t("sessions.no-tool-runs"))
            return
        self._show_inspector(history[-1].id)

    def _replay_task(self, task_id: str) -> None:
        self.release_task(task_id)
        self._write_dim(self.translator.t("sessions.replayed", task=task_id))

    def _child_tool_runs(self, run: AgentRun) -> list[ToolRun]:
        """Tool runs caused by this subagent, oldest first.

        Tool runs carry the plan task, not the agent run, so the task plus the
        subagent's start time is the attribution we have.
        """
        if self.operation is None:
            return []
        return [
            item
            for item in self.execution.list_runs(self.operation.id)
            if item.task_id == run.task_id and item.created_at >= run.created_at
        ]

    def _session_sections(self, run: AgentRun) -> list[tuple[str, list[str]]]:
        t = self.translator
        steps: list[str] = []
        notes: list[str] = []
        findings: list[str] = []
        if self.operation is not None:
            for event in self.service.events(self.operation.id):
                payload = event.payload
                if payload.get("run_id") != run.id:
                    continue
                if event.event_type == "agent.note":
                    notes.append(f"{payload.get('key')} = {str(payload.get('value'))[:200]}")
                elif event.event_type == "agent.step":
                    if payload.get("finding_id"):
                        findings.append(str(payload.get("title")))
                        continue
                    steps.append(str(payload.get("title") or payload.get("step")))
        calls = [item.id for item in self._child_tool_runs(run)]
        return [
            (
                t.t("sessions.section.summary"),
                [
                    t.t("sessions.status", status=run.status.value, depth=run.depth),
                    t.t("sessions.instruction", task=run.task_id),
                ],
            ),
            (t.t("sessions.section.steps"), steps),
            (t.t("sessions.section.calls"), calls),
            (t.t("sessions.section.notes"), notes),
            (t.t("sessions.section.findings"), findings),
        ]

    def _copy_text(self, text: str, note: str) -> None:
        self.copy_to_clipboard(text)
        self._write_dim(note)

    @staticmethod
    def _run_command_text(run: ToolRun) -> str:
        parameters = getattr(run, "parameters", {}) or {}
        if "command" in parameters:
            return str(parameters["command"])
        if "argv" in parameters:
            return " ".join(str(part) for part in parameters["argv"])
        if "url" in parameters:
            return str(parameters["url"])
        return ""

    def _start_shell_mode(self, command: str) -> None:
        self._shell_mode(command)

    def _inspector_sections(
        self, run: ToolRun, definition: ToolDefinition
    ) -> list[tuple[str, list[str]]]:
        t = self.translator
        lines: list[tuple[str, list[str]]] = []
        lines.append(
            (
                t.t("inspector.section.definition"),
                [
                    t.t(
                        "inspector.risk",
                        risk=definition.risk_class.value,
                        network=definition.network.value,
                        read_only=str(definition.read_only).lower(),
                    ),
                    t.t(
                        "inspector.status",
                        status=run.status.value,
                        code=run.exit_code,
                    ),
                    getattr(definition, "description", ""),
                ],
            )
        )
        parameters = getattr(run, "parameters", {}) or {}
        lines.append(
            (
                t.t("inspector.section.parameters"),
                [f"{key} = {value}" for key, value in parameters.items()],
            )
        )
        lines.append(
            (
                t.t("inspector.section.scope"),
                self._scope_lines(parameters),
            )
        )
        lines.append((t.t("inspector.section.approval"), self._run_approval_lines(run)))
        lines.append((t.t("inspector.section.egress"), self._run_egress_lines(run)))
        lines.append((t.t("inspector.section.artifacts"), self._run_artifact_lines(run)))
        output = self._run_output_lines(run)
        lines.append((t.t("inspector.section.output"), output))
        warnings = len(getattr(run, "injection_warnings", ()))
        lines.append(
            (
                t.t("inspector.section.trust"),
                [
                    t.t(
                        "inspector.trust-line",
                        trust=run.trust.value,
                        warnings=t.t("inspector.warnings", count=warnings) if warnings else "",
                    )
                ],
            )
        )
        return lines

    def _scope_lines(self, parameters: dict[str, object]) -> list[str]:
        targets: list[str] = []
        for key in ("targets", "url", "target"):
            value = parameters.get(key)
            if isinstance(value, list):
                targets.extend(str(item) for item in value)
            elif value:
                targets.append(str(value))
        if self.operation is None:
            return []
        lines: list[str] = []
        for target in targets:
            decision = self.operation.scope.evaluate(target)
            verdict = "allow" if decision.allowed else "deny"
            lines.append(f"{verdict} {target} — {decision.detail}")
        return lines

    def _run_approval_lines(self, run: ToolRun) -> list[str]:
        approval_id = getattr(run, "approval_id", None)
        if approval_id is None:
            return []
        approval = self.kernel.kernel.get_approval(approval_id)
        if approval is None:
            return [approval_id]
        return [
            self.translator.t(
                "inspector.approval-line",
                action=approval.action,
                status=approval.status.value,
                who=approval.decided_by or "-",
            )
        ]

    def _run_egress_lines(self, run: ToolRun) -> list[str]:
        operation = self.operation
        if operation is None:
            return []
        run_id = run.id
        lines: list[str] = []
        for event in self.service.repository.events(operation.id):
            if not event.event_type.startswith("tool.run."):
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            if payload.get("run_id") != run_id:
                continue
            host = payload.get("host")
            if host is None:
                continue
            lines.append(
                self.translator.t(
                    "inspector.egress-line",
                    verdict=payload.get("verdict", "?"),
                    host=host,
                    port=payload.get("port", "?"),
                    bytes=payload.get("bytes", 0),
                )
            )
        return lines

    def _run_artifact_lines(self, run: ToolRun) -> list[str]:
        operation = self.operation
        if operation is None:
            return []
        wanted = {
            getattr(run, "stdout_artifact_id", None),
            getattr(run, "stderr_artifact_id", None),
        }
        wanted.discard(None)
        lines: list[str] = []
        for artifact in self.kernel.kernel.list_artifacts(operation.id):
            if artifact.id not in wanted:
                continue
            lines.append(
                self.translator.t(
                    "inspector.artifact-line",
                    kind=artifact.kind.value,
                    id=artifact.id,
                    sha256=artifact.sha256,
                    size=artifact.size_bytes,
                )
            )
        return lines

    def _run_output_lines(self, run: ToolRun) -> list[str]:
        lines: list[str] = []
        for attr in ("stdout_artifact_id", "stderr_artifact_id"):
            artifact_id = getattr(run, attr, None)
            if artifact_id is None or self.operation is None:
                continue
            artifact = next(
                (
                    item
                    for item in self.kernel.kernel.list_artifacts(self.operation.id)
                    if item.id == artifact_id
                ),
                None,
            )
            if artifact is None:
                continue
            for line in self._read_artifact(artifact):
                lines.append(line)
        return lines

    def _read_artifact(self, artifact: Artifact) -> list[str]:
        try:
            text = Path(artifact.path).read_text(errors="replace")
        except OSError:
            return []
        return text.rstrip().splitlines()[-40:]

    def action_show_help(self) -> None:
        self._show_help()

    def _show_help(self) -> None:
        self.push_screen(HelpScreen(self.translator, self.keymap))

    def _cmd_exit(self) -> None:
        # ``_exit`` is already Textual's own hook, so the command differs
        self.exit()

    def _prompt_trust(self) -> None:
        self._maybe_prompt_trust(force=True)

    def _set_mode_ask(self) -> None:
        self._set_permission_mode(PermissionMode.ASK)

    def _set_mode_never(self) -> None:
        self._set_permission_mode(PermissionMode.NEVER)

    def _compact_now(self) -> None:
        self.run_worker(self._compact_history(), exclusive=False)

    def _show_status(self) -> None:
        if not self.operation:
            self._write_line("[status] " + self.translator.t("status.no-operation"))
            return
        self.operation = self.service.get(self.operation.id)
        operation = self.operation
        summary = self.kernel.summary(operation.id)
        self._write_line(
            f"[status] {operation.id} | {operation.name} | {operation.mode.value} | "
            f"{operation.status.value} | version={operation.version}"
        )
        self._write_line(
            f"[kernel] tasks={summary.tasks_done}/{summary.tasks_total} | "
            f"gates={summary.gates_passed}/{summary.gates_total} | "
            f"pending-approvals={summary.pending_approvals} | "
            f"artifacts={summary.artifacts} | findings={summary.findings}"
        )
        self._refresh_chrome()

    def _show_scope(self, target: str) -> None:
        if not self.operation:
            self._write_line("[scope] " + self.translator.t("status.no-operation"))
            return
        scope = self.operation.scope
        if not target:
            self._write_line(
                "[scope] " + self.translator.t("scope.includes", rules=", ".join(scope.includes))
            )
            excludes = ", ".join(scope.excludes) or self.translator.t("scope.none")
            self._write_line("[scope] " + self.translator.t("scope.excludes", rules=excludes))
            return
        decision = scope.evaluate(target)
        level = "allow" if decision.allowed else "deny"
        self._write_line(
            f"[{level}] {decision.target}: {decision.reason.value} — {decision.detail}"
        )

    def _plan_lines(self) -> list[str]:
        if not self.operation:
            return ["[plan] " + self.translator.t("status.no-operation")]
        operation = self.operation
        objective = operation.objective or self.translator.t("plan.objective-unset")
        plan = self.kernel.get_current_plan(operation.id)
        lines = [
            "[plan] "
            + self.translator.t("plan.stage", stage=operation.status.value, objective=objective)
        ]
        if plan is None:
            lines.append("[plan] " + self.translator.t("plan.no-plan"))
            return lines
        approved = self.translator.t("plan.approved" if plan.approved else "plan.not-approved")
        tasks_done = sum(
            1 for task in plan.tasks if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
        )
        gates_passed = sum(1 for gate in plan.gates if gate.status == GateStatus.PASSED)
        lines.append(
            f"[plan] {plan.id} | {plan.title} | {approved} | version={plan.version} | "
            f"tasks={tasks_done}/{len(plan.tasks)} | gates={gates_passed}/{len(plan.gates)}"
        )
        return lines

    def _show_plan(self) -> None:
        for line in self._plan_lines():
            self._write_line(line)

    def _task_lines(self) -> list[str]:
        if not self.operation:
            return ["[tasks] " + self.translator.t("status.no-operation")]
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.tasks:
            return ["[tasks] " + self.translator.t("tasks.empty")]
        lines: list[str] = []
        for task in plan.tasks:
            dependencies = ",".join(task.depends_on) or "-"
            requirements = ",".join(task.required_artifacts) or "-"
            lines.append(
                f"[task] {task.id} | {task.status.value} | risk={task.risk_class.value} | "
                f"deps={dependencies} | artifacts={requirements} | {task.title}"
            )
        return lines

    def _show_tasks(self) -> None:
        for line in self._task_lines():
            self._write_line(line)

    def _gate_lines(self) -> list[str]:
        if not self.operation:
            return ["[gates] " + self.translator.t("status.no-operation")]
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.gates:
            return ["[gates] " + self.translator.t("gates.empty")]
        lines: list[str] = []
        for gate in plan.gates:
            requirements = ",".join(gate.required_artifacts) or "-"
            lines.append(
                f"[gate] {gate.id} | {gate.status.value} | artifacts={requirements} | {gate.name}"
            )
        return lines

    def _show_gates(self) -> None:
        for line in self._gate_lines():
            self._write_line(line)

    def _finding_lines(self) -> list[Text]:
        if not self.operation:
            return [self._styled("[findings] " + self.translator.t("status.no-operation"))]
        findings = self.kernel.kernel.list_findings(self.operation.id)
        if not findings:
            return [self._styled("[findings] " + self.translator.t("findings.empty"))]
        lines: list[Text] = []
        for finding in findings:
            duplicate = f" | duplicate-of={finding.duplicate_of}" if finding.duplicate_of else ""
            lines.append(
                Text.assemble(
                    ("[finding] ", self.paint("accent-alt")),
                    entity_span(finding.id, "finding", finding.id, base_style=self.paint("accent")),
                    (
                        f" | {finding.severity.value} | {finding.status.value} | "
                        f"evidence={len(finding.evidence_ids)}{duplicate} | {finding.title}",
                        self.paint("$text"),
                    ),
                )
            )
        return lines

    def _show_findings(self) -> None:
        journal = self.query_one("#journal", BlockJournal)
        for line in self._finding_lines():
            journal.write(line)

    def _evidence_lines(self) -> list[str | Text]:
        if not self.operation:
            return ["[evidence] " + self.translator.t("status.no-operation")]
        artifacts = self.kernel.kernel.list_artifacts(self.operation.id)
        if not artifacts:
            return ["[evidence] " + self.translator.t("evidence.empty")]
        lines: list[str | Text] = []
        for artifact in artifacts:
            lines.append(
                Text.assemble(
                    ("[artifact] ", self.paint("text-faint")),
                    entity_span(
                        artifact.id, "artifact", artifact.id, base_style=self.paint("accent")
                    ),
                    (
                        f" | {artifact.kind.value} | "
                        f"requirement={artifact.requirement or '-'} | "
                        f"links={_artifact_links(artifact)} | sha256={artifact.sha256[:12]} | "
                        f"{artifact.summary or artifact.path}",
                        self.paint("$text"),
                    ),
                )
            )
        return lines

    def _show_evidence(self) -> None:
        journal = self.query_one("#journal", BlockJournal)
        for line in self._evidence_lines():
            journal.write(line)

    def _approval_lines(self) -> list[str | Text]:
        if not self.operation:
            return ["[approvals] " + self.translator.t("status.no-operation")]
        approvals = self.kernel.kernel.list_approvals(self.operation.id)
        if not approvals:
            return ["[approvals] " + self.translator.t("approvals.empty")]
        lines: list[str | Text] = []
        for approval in approvals:
            actor = approval.decided_by or "-"
            lines.append(
                Text.assemble(
                    ("[approval] ", self.paint("warning")),
                    entity_span(
                        approval.id, "approval", approval.id, base_style=self.paint("accent")
                    ),
                    (
                        f" | {approval.risk_class.value} | "
                        f"{approval.status.value} | auto={str(approval.auto).lower()} | "
                        f"actor={actor} | {approval.action}",
                        self.paint("$text"),
                    ),
                )
            )
        return lines

    def _show_approvals(self) -> None:
        journal = self.query_one("#journal", BlockJournal)
        for line in self._approval_lines():
            journal.write(line)

    def _show_snapshots(self) -> None:
        if not self.operation:
            self._write_line("[snapshots] " + self.translator.t("status.no-operation"))
            return
        snapshots = self.recovery.list_snapshots(self.operation.id)
        if not snapshots:
            self._write_line("[snapshots] " + self.translator.t("snapshots.empty"))
            return
        for snapshot in snapshots:
            self._write_line(
                f"[snapshot] {snapshot.id} | {snapshot.label or '-'} | "
                f"{snapshot.created_at.isoformat()}"
            )

    def _show_tools(self) -> None:
        definitions = self.execution.list_tools()
        if not definitions:
            self._write_line("[tools] " + self.translator.t("tools.empty"))
            return
        for definition in definitions:
            network = self.translator.t(f"tools.network.{definition.network.value}")
            self._write_line(
                f"[tool] {definition.id} | v{definition.version} | {definition.kind.value} | "
                f"risk={definition.risk_class.value} | {network} | {definition.description}"
            )

    def _show_runs(self) -> None:
        if not self.operation:
            self._write_line("[runs] " + self.translator.t("status.no-operation"))
            return
        runs = self.execution.list_runs(self.operation.id)
        if not runs:
            self._write_line("[runs] " + self.translator.t("runs.empty"))
            return
        for run in runs:
            artifacts = (
                ",".join(
                    artifact_id
                    for artifact_id in (run.stdout_artifact_id, run.stderr_artifact_id)
                    if artifact_id
                )
                or "-"
            )
            warnings = ",".join(run.injection_warnings) or "-"
            self._write_line(
                f"[run] {run.id} | {run.tool_id} v{run.tool_version} | {run.status.value} | "
                f"trust={run.trust.value} | warnings={warnings} | artifacts={artifacts}"
            )

    def _show_agents(self) -> None:
        if not self.operation:
            self._write_line("[agents] " + self.translator.t("status.no-operation"))
            return
        runs = self.agents.list_runs(self.operation.id)
        if not runs:
            self._write_line("[agents] " + self.translator.t("agents.empty"))
            return
        for run in runs:
            self._write_line(
                f"[agent] {run.id} | {run.task_id} | {run.status.value} | "
                f"depth={run.depth} | route={','.join(run.model_route)}"
            )

    def _show_swarms(self) -> None:
        if not self.operation:
            self._write_line("[swarm] " + self.translator.t("status.no-operation"))
            return
        swarms = self.swarms.list_swarms(self.operation.id)
        if not swarms:
            self._write_line("[swarm] " + self.translator.t("swarm.empty"))
            return
        for swarm in swarms:
            self._write_line(
                f"[swarm] {swarm.id} | {swarm.status.value} | workers={swarm.workers} | "
                f"tasks={','.join(swarm.task_ids)}"
            )
            for member in self.agents.list_swarm_members(swarm.id):
                self._write_line(
                    f"  [member] {member.task_id} | {member.id} | {member.status.value} | "
                    f"{member.specialization.value}"
                )

    def _handle_constraint_command(self, argument: str) -> None:
        """`/constraint [deny|allow|rm] <text|id>`, listing when bare."""
        if self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        rows = self.constraints.in_operation(self.operation.id)
        parts = argument.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            if action in ("deny", "allow") and rest:
                if plan is None:
                    self._write_line(self.translator.t("constraint.no-plan"))
                    return
                constraint = self.constraints.add(
                    self.operation.id, str(plan.tasks[0].id), ConstraintKind(action), rest
                )
                self._write_dim(self.translator.t("constraint.added", text=constraint.text))
                return
            if action == "rm" and rest:
                self.constraints.remove(rest)
                self._write_dim(self.translator.t("constraint.removed", id=rest))
                return
        except Exception as exc:  # noqa: BLE001 - command feedback, never a crash
            self._write_line(f"[constraint] {exc}")
            return
        if not rows:
            self._write_dim(self.translator.t("constraint.empty"))
            return
        for row in rows:
            self._write_line(f"[constraint] {row.id} | {row.kind.value} | {row.text}")

    def _handle_retest_command(self, argument: str) -> None:
        """`/retest <finding>` starts; `/retest verdict <id> <verdict> <summary> | <evidence>
        [| control]` closes; `/retest <finding>` with history lists it."""
        parts = argument.split(maxsplit=1)
        action = parts[0].strip().lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            if action == "verdict" and rest:
                fields = [part.strip() for part in rest.split("|")]
                head = fields[0].split(maxsplit=2)
                if len(head) < 3:
                    self._write_line(self.translator.t("retest.usage"))
                    return
                retest_id, verdict, summary = head[0], head[1], head[2]
                evidence = fields[1] if len(fields) > 1 else ""
                control = fields[2] if len(fields) > 2 else ""
                finished = self.retests.finish(
                    retest_id, RetestVerdict(verdict), summary, evidence, control
                )
                self._write_dim(
                    self.translator.t(
                        "retest.finished-pair" if finished.control_pair else "retest.finished",
                        id=finished.id,
                        verdict=verdict,
                        pair=finished.control_pair,
                    )
                )
                self._refresh_chrome()
                return
            if action:
                started = self.retests.start(action)
                self._write_line(self.translator.t("retest.rules"))
                self._write_dim(
                    self.translator.t("retest.started", id=started.id, finding=started.finding_id)
                )
                return
        except Exception as exc:  # noqa: BLE001 - command feedback, never a crash
            self._write_line(f"[retest] {exc}")
            return
        self._write_line(self.translator.t("retest.usage"))

    def _handle_traffic_command(self, argument: str) -> None:
        """`/traffic` lists captured pairs; `/traffic bind <pair> <finding> [role]` binds."""
        if self.operation is None:
            self._write_line("[error] " + self.translator.t("palette.no-operation"))
            return
        parts = argument.split()
        try:
            if parts and parts[0] == "bind" and len(parts) >= 3:
                role = TrafficRole(parts[3]) if len(parts) > 3 else TrafficRole.SUPPORTING
                finding = self.traffic.bind(parts[2], parts[1], role)
                self._write_dim(
                    self.translator.t(
                        "traffic.bound",
                        pair=parts[1],
                        finding=finding.id,
                        role=role.value,
                    )
                )
                return
        except Exception as exc:  # noqa: BLE001 - command feedback, never a crash
            self._write_line(f"[traffic] {exc}")
            return
        pairs = self.traffic.for_operation(self.operation.id)
        if not pairs:
            self._write_dim(self.translator.t("traffic.empty"))
            return
        for pair in pairs:
            self._write_line(
                f"[traffic] {pair.id} | {pair.method} {pair.url} | {pair.status}"
                f"{' | clipped' if pair.truncated else ''}"
            )

    def _handle_skills_gaps(self) -> None:
        rows = self.skills.gaps_report(self._memory_workspace())
        if not rows:
            self._write_dim(self.translator.t("skills.no-gaps"))
            return
        for row in rows:
            self._write_line(f"[gap] {row.skill} | misses={row.misses} | last={row.last_seen}")

    def _handle_lessons_command(self, argument: str) -> None:
        """`/lessons [extract|adopt <id>|reject <id>|mode <mode>]`, listing by default."""
        workspace = self._memory_workspace()
        parts = argument.split()
        action = parts[0].lower() if parts else ""
        target = parts[1].strip() if len(parts) > 1 else ""
        try:
            if action == "extract":
                if self.operation is None:
                    self._write_line("[error] " + self.translator.t("palette.no-operation"))
                    return
                admitted = self.lessons.extract(self.operation.id, workspace)
                self._write_dim(self.translator.t("lessons.extracted", count=len(admitted)))
                return
            if action in ("adopt", "reject") and target:
                lesson = (
                    self.lessons.adopt(target) if action == "adopt" else self.lessons.reject(target)
                )
                self._write_dim(
                    self.translator.t(
                        "lessons.status", lesson=lesson.id, status=lesson.status.value
                    )
                )
                return
            if action == "mode" and target:
                self.lessons.set_mode(LessonMode(target))
                self._write_dim(self.translator.t("lessons.mode", mode=target))
                return
        except Exception as exc:  # noqa: BLE001 - command feedback, never a crash
            self._write_line(f"[lessons] {exc}")
            return
        lessons = self.lessons.all(workspace)
        if not lessons:
            self._write_dim(self.translator.t("lessons.empty"))
            return
        self._write_dim(self.translator.t("lessons.mode-line", mode=self.lessons.mode().value))
        for lesson in lessons:
            self._write_line(
                f"[lesson] {lesson.id} | {lesson.status.value} | {lesson.kind.value} | "
                f"{self.lessons.support(lesson.id)} | {lesson.cue} -> {lesson.action}"
            )

    def _log_shadow_lessons(self) -> None:
        """In shadow mode, record what `inject` would have shown — once per turn."""
        if self.operation is None or self.lessons.mode() is not LessonMode.SHADOW:
            return
        candidates = self.lessons.shadow_candidates(self._memory_workspace())
        if not candidates:
            return
        self.service.repository.append_event(
            OperationEvent(
                operation_id=self.operation.id,
                event_type="lesson.shadow",
                payload={"lesson_ids": list(candidates)},
            )
        )
        self._write_dim(self.translator.t("lessons.would-show", count=len(candidates)))

    def _show_memory(self, workspace: str) -> None:
        entries = self.memory.list_entries(workspace.strip() or None)
        if not entries:
            self._write_line("[memory] " + self.translator.t("memory.empty"))
            return
        for entry in entries:
            self._write_line(
                f"[memory] {entry.id} | {entry.workspace} | {entry.kind.value} | {entry.title}"
            )

    def _handle_rules_command(self, argument: str = "") -> None:
        """`/rules` lists; `/rules add <text>` and `/rules rm <text>` edit."""
        text = argument.strip()
        verb, _, rest = text.partition(" ")
        try:
            if verb == "add" and rest.strip():
                rule = self.settings.add_permission_rule(rest.strip())
                self._write_line(f"[rules] added {rule.format()}")
                return
            if verb == "rm" and rest.strip():
                removed = self.settings.remove_permission_rule(rest.strip())
                self._write_line(
                    f"[rules] {'removed' if removed else 'no such rule'} {rest.strip()}"
                )
                return
        except PermissionRuleError as exc:
            self._write_line(f"[rules] {exc}")
            return
        rules = self.settings.permission_rules()
        if not rules:
            self._write_line("[rules] no rules")
            return
        for rule in rules:
            self._write_line(f"[rules] {rule.format()}")

    def _show_skills(self, argument: str = "") -> None:
        if argument.strip().lower() == "gaps":
            self._handle_skills_gaps()
            return
        installed = self.skills.list_skills()
        if not installed:
            self._write_line("[skills] " + self.translator.t("skills.empty"))
            return
        for skill in installed:
            self._write_line(f"[skill] {skill.name} | v{skill.version}")

    def _show_custom_playbooks(self) -> None:
        templates = self.playbooks.list_custom()
        if not templates:
            self._write_line("[playbooks] " + self.translator.t("playbooks.custom-empty"))
            return
        for template in templates:
            self._write_line(
                f"[playbook] {template.name} | {template.title} | "
                f"tasks={len(template.tasks)} gates={len(template.gates)}"
            )

    def _show_secrets(self) -> None:
        operation_id = self.operation.id if self.operation else None
        entries = self.vault.list_metadata(operation_id)
        if not entries:
            self._write_line("[secrets] " + self.translator.t("secrets.empty"))
            return
        for entry in entries:
            scope = entry.operation_id or self.translator.t("secrets.global")
            self._write_line(
                f"[secret] {entry.id} | {entry.name} | scope={scope} | "
                f"by={entry.created_by} | {entry.created_at.isoformat()}"
            )

    def _show_assets(self) -> None:
        if not self.operation:
            self._write_line("[assets] " + self.translator.t("status.no-operation"))
            return
        entries = self.playbooks.list_assets(self.operation.id)
        if not entries:
            self._write_line("[assets] " + self.translator.t("assets.empty"))
            return
        for entry in entries:
            self._write_line(
                f"[asset] {entry.id} | {entry.kind.value} | {entry.value} | {entry.source}"
            )

    def _show_reports(self) -> None:
        if not self.operation:
            self._write_line("[report] " + self.translator.t("status.no-operation"))
            return
        reports = self.reports.reports.list_reports(self.operation.id)
        if not reports:
            self._write_line("[report] " + self.translator.t("report.empty"))
            return
        for report in reports:
            self._write_line(
                f"[report] {report.id} | {report.format.value} | "
                f"findings={len(report.finding_ids)} | {report.created_at.isoformat()}"
            )

    def _show_hypotheses(self) -> None:
        if not self.operation:
            self._write_line("[hypotheses] " + self.translator.t("status.no-operation"))
            return
        hypotheses = self.ctf.list_hypotheses(self.operation.id)
        if not hypotheses:
            self._write_line("[hypotheses] " + self.translator.t("hypotheses.empty"))
            return
        for item in hypotheses:
            self._write_line(f"[hypothesis] {item.id} | {item.status.value} | {item.title}")

    def _show_flags(self) -> None:
        if not self.operation:
            self._write_line("[flags] " + self.translator.t("status.no-operation"))
            return
        flags = self.ctf.list_flags(self.operation.id)
        if not flags:
            self._write_line("[flags] " + self.translator.t("flags.empty"))
            return
        for flag in flags:
            self._write_line(f"[flag] {flag.id} | {flag.name} | {flag.value}")

    def _show_attack_path(self) -> None:
        if not self.operation:
            self._write_line("[attack-path] " + self.translator.t("status.no-operation"))
            return
        steps = self.redteam.list_steps(self.operation.id)
        if not steps:
            self._write_line("[attack-path] " + self.translator.t("attack-path.empty"))
            return
        for step in steps:
            self._write_line(
                f"[step] {step.id} | {step.status.value} | {step.technique_id} | "
                f"{step.name} | detection={step.detection.value}"
            )

    def _show_debriefs(self) -> None:
        if not self.operation:
            self._write_line("[debrief] " + self.translator.t("status.no-operation"))
            return
        debriefs = self.redteam.list_debriefs(self.operation.id)
        if not debriefs:
            self._write_line("[debrief] " + self.translator.t("debrief.empty"))
            return
        for debrief in debriefs:
            self._write_line(
                f"[debrief] {debrief.id} | {debrief.format.value} | "
                f"steps={debrief.step_count} | gaps={len(debrief.open_gaps)} | "
                f"{debrief.created_at.isoformat()}"
            )

    def _show_test_cases(self) -> None:
        if not self.operation:
            self._write_line("[test-cases] " + self.translator.t("status.no-operation"))
            return
        cases = self.planning.list_test_cases(self.operation.id)
        if not cases:
            self._write_line("[test-cases] " + self.translator.t("test-cases.empty"))
            return
        for case in cases:
            marker = " | STOP" if case.stop_before else ""
            self._write_line(
                f"[test-case] {case.id} | {case.status.value} | "
                f"{case.technique_id} | {case.kill_chain_phase.value} | "
                f"{case.name}{marker}"
            )

    def _show_runbooks(self) -> None:
        if not self.operation:
            self._write_line("[runbook] " + self.translator.t("status.no-operation"))
            return
        runbooks = self.planning.list_runbooks(self.operation.id)
        if not runbooks:
            self._write_line("[runbook] " + self.translator.t("runbook.empty"))
            return
        for runbook in runbooks:
            self._write_line(
                f"[runbook] {runbook.id} | {runbook.format.value} | "
                f"cases={len(runbook.test_case_ids)} | stops={runbook.stop_points} | "
                f"{runbook.created_at.isoformat()}"
            )

    def _show_reconcile(self) -> None:
        if not self.operation:
            self._write_line("[reconcile] " + self.translator.t("status.no-operation"))
            return
        marked_runs = self.execution.reconcile_runs(self.operation.id, actor="operator")
        report = self.recovery.reconcile_operation(self.operation.id, actor="operator")
        none = self.translator.t("list.none")
        self._write_line(
            "[reconcile] "
            + self.translator.t(
                "reconcile.report",
                actions=", ".join(report.actions_marked_unknown) or none,
                runs=", ".join(marked_runs) or none,
                tasks=", ".join(report.resumable_tasks) or none,
                approvals=", ".join(report.pending_approvals) or none,
            )
        )

    def _show_lang(self, argument: str) -> None:
        if not argument:
            self._write_line(
                "[lang] " + self.translator.t("lang.current", locale=self.translator.locale)
            )
            return
        try:
            locale = self.settings.set_locale(argument)
        except InvalidLocaleError:
            self._write_line("[error] " + self.translator.t("lang.invalid", locale=argument))
            return
        self.translator.set_locale(locale)
        self.query_one("#command-input", Input).placeholder = self.translator.t("input.placeholder")
        self._retitle_tabs()
        self._write_line("[lang] " + self.translator.t("lang.changed", locale=locale))
        self._refresh_chrome()

    def _short_cwd(self) -> str:
        text = str(self._cwd)
        home = str(Path.home())
        if text.startswith(home):
            text = "~" + text[len(home) :]
        return text

    def _refresh_swarm_panel(self) -> None:
        """Every running member of the current swarm: task, tool, last note."""
        panel = self.query_one("#side-swarm", Static)
        if self.operation is None:
            panel.update("")
            return
        runs = self.agents.list_runs(self.operation.id)
        active = [run for run in runs if run.status is AgentRunStatus.RUNNING]
        if not active:
            panel.update("")
            return
        lines = Text()
        lines.append(self.translator.t("side.swarm") + "\n", style=self.paint("dim accent"))
        for run in active[:6]:
            lines.append("▶ ", style=self.paint("risk-mid"))
            lines.append(f"{run.task_id or run.id[:10]}", style=self.paint("text"))
            tool = self._agent_now_text(run.task_id or "")
            if tool:
                lines.append(f"  {tool.strip()}", style=self.paint("dim text-faint"))
            lines.append("\n")
        panel.update(lines)

    def _refresh_side_blocks(self) -> None:
        """Hosts we hold access to, and the names of vaulted secrets."""
        from samurai_ai.tui.killchain import access_rows

        access = self.query_one("#side-access", Static)
        creds = self.query_one("#side-creds", Static)
        if self.operation is None:
            access.update("")
            creds.update("")
            return
        facts = [
            str(node.payload.get("summary") or node.text())
            for node in self.graph.for_operation(self.operation.id)
            if node.kind is NodeKind.FACT and node.state is NodeState.CONFIRMED
        ]
        # hosts the agent actually touched count too: a hit is access at read
        # level even before a fact is recorded, otherwise the block never moves
        touched = [
            str(run.parameters.get("command", ""))
            for run in self.execution.list_runs(self.operation.id)
            if run.exit_code == 0
        ]
        rows = access_rows([*facts, *touched[-40:]])
        lines = Text()
        lines.append(self.translator.t("side.access") + "\n", style=self.paint("dim accent"))
        if not rows:
            lines.append(
                self.translator.t("side.access-empty"), style=self.paint("dim text-faint")
            )
        for row in rows[:6]:
            token = {"root": "success", "user": "risk-mid"}.get(row.level, "dim text-faint")
            lines.append("● ", style=self.paint(token))
            lines.append(row.host, style=self.paint("text"))
            lines.append(f" {row.level}\n", style=self.paint(token))
        access.update(lines)

        names = [meta.name for meta in self.vault.list_metadata(self.operation.id)]
        secrets = Text()
        secrets.append(self.translator.t("side.creds") + "\n", style=self.paint("dim accent"))
        if not names:
            secrets.append(
                self.translator.t("side.creds-empty"), style=self.paint("dim text-faint")
            )
        for name in names[:6]:
            secrets.append("· ", style=self.paint("dim"))
            secrets.append(name + "\n", style=self.paint("text"))
        creds.update(secrets)

    def _killchain(self) -> Any:
        """The stage rail for the current operation; empty when there is none."""
        from samurai_ai.tui.killchain import KillChain, nodes_to_payloads, phase_for

        if self.operation is None:
            return KillChain((), "", "", 0, 0)
        plan = self.kernel.get_current_plan(self.operation.id)
        tasks = list(plan.tasks) if plan else []
        gate_total = len(plan.gates) if plan else 0
        gate_passed = sum(1 for gate in plan.gates if gate.status.value == "passed") if plan else 0
        findings = self.kernel.kernel.list_findings(self.operation.id)
        verified = sum(1 for f in findings if f.evidence_ids)
        flags = self.ctf.list_flags(self.operation.id)
        challenge = self._challenge_flag_total()
        facts = nodes_to_payloads(self.graph.for_operation(self.operation.id))
        levels = self._impact_levels(self.operation.id)
        return phase_for(
            self.operation.mode,
            tasks,
            fact_payloads=facts,
            impact_levels=levels,
            flag_count=len(flags),
            flag_total=challenge,
            verified_findings=verified,
            gate_passed=gate_passed,
            gate_total=gate_total,
            chain=tuple(gate.id for gate in plan.gates) if plan and plan.gates else None,
        )

    def _challenge_flag_total(self) -> int:
        """How many flags the challenge has, when the operation is one."""
        try:
            challenge = self.ctf.get_challenge(self.operation.id)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - a non-CTF operation has none
            return 0
        return 2 if challenge is not None else 0

    def _impact_levels(self, operation_id: str) -> list[str]:
        return [
            str(event.payload.get("level"))
            for event in self.service.events(operation_id)
            if event.event_type == "impact.attempt" and event.payload.get("level")
        ]

    def _phase_label(self, phase: str) -> str:
        """Stage names stay English in every locale — they are terms of art."""
        return phase.replace("-", " ")


    def _refresh_attack_map(self) -> None:
        """The access graph, when there is more than one host to name."""
        from samurai_ai.tui.killchain import access_rows, attack_map

        panel = self.query_one("#attack-map", Static)
        if self.operation is None:
            panel.display = False
            return
        facts = [
            str(node.payload.get("summary") or node.text())
            for node in self.graph.for_operation(self.operation.id)
            if node.kind is NodeKind.FACT and node.state is NodeState.CONFIRMED
        ]
        rows = access_rows(facts)
        if len(rows) < 2:
            panel.display = False
            return
        panel.display = True
        panel.update("\n".join(attack_map(rows)))

    def _refresh_operations_strip(self) -> None:
        """One row of open operations; the current one is marked."""
        strip = self.query_one("#operations-strip", Horizontal)
        if not self._open_tabs:
            strip.display = False
            return
        strip.display = True
        strip.remove_children()
        current_id = self.operation.id if self.operation else ""
        for op_id in self._open_tabs:
            try:
                operation = self.service.get(op_id)
            except OperationNotFoundError:
                continue
            label = operation.name or op_id
            busy = op_id != current_id and any(
                run.status is AgentRunStatus.RUNNING
                for run in self.agents.list_runs(op_id)
            )
            tab = OperationTab(
                op_id,
                label,
                busy=busy,
                current=op_id == current_id,
            )
            tab.add_class("operation-tab")
            if op_id == current_id:
                tab.add_class("operation-tab-current")
            strip.mount(tab)

    def _close_operation_by_id(self, operation_id: str) -> None:
        """Right-click on the strip closes that tab; the operation stays."""
        if self.operation is not None and self.operation.id == operation_id:
            self._close_operation()
            return
        self._open_tabs = [op for op in self._open_tabs if op != operation_id]
        self._tab_states.pop(operation_id, None)
        self._refresh_operations_strip()

    def _refresh_killchain(self) -> None:
        widget = self.query_one("#killchain", Static)
        chain = self._killchain()
        if not chain:
            widget.display = False
            return
        widget.display = True
        line = Text()
        for phase in chain.phases:
            if phase == chain.current:
                line.append(f"▶ {self._phase_label(phase)} ",
                            style=self.paint("bold risk-mid"))
            else:
                done = chain.phases.index(phase) < chain.phases.index(chain.current)
                mark = "✓" if done else "·"
                line.append(f"{mark} {self._phase_label(phase)} ",
                            style=self.paint("success" if done else "dim text-faint"))
        if chain.counter_label:
            total = f"/{chain.counter_total}" if chain.counter_total else ""
            line.append(
                f"│ {self.translator.t('killchain.counter.' + chain.counter_label)} "
                f"{chain.counter_done}{total}",
                style=self.paint("accent"),
            )
        widget.update(line)
        if chain.current and chain.current != self._killchain_phase:
            previous, self._killchain_phase = self._killchain_phase, chain.current
            if previous:
                self._write_dim(
                    self.translator.t(
                        "killchain.moved",
                        **{
                            "from": self._phase_label(previous),
                            "to": self._phase_label(chain.current),
                        },
                    ),
                    process=False,
                )

    def _refresh_welcome(self) -> None:
        welcome = self.query_one("#welcome", Static)
        if self.operation is not None:
            welcome.display = False
            return
        welcome.display = True
        latest = self.service.latest()
        lines = Text()
        lines.append(self.translator.t("welcome.title"), style=self.paint("bold $accent"))
        lines.append("\n\n")
        lines.append(self.translator.t("welcome.palette"), style=self.paint("$text"))
        lines.append("\n")
        lines.append(self.translator.t("welcome.new"), style=self.paint("$text"))
        if latest is not None:
            lines.append("\n")
            lines.append(
                self.translator.t("welcome.last", name=latest.name), style=self.paint("$text")
            )
        lines.append("\n\n")
        lines.append(self.translator.t("welcome.hint"), style=self.paint("$text-dim"))
        welcome.update(lines)

    def _open_latest_operation(self) -> None:
        latest = self.service.latest()
        if latest is None:
            self._write_line(self.translator.t("welcome.no-last"))
            return
        self.operation = latest
        self._chat_history.clear()
        self.query_one("#journal", BlockJournal).clear()
        self._write_welcome()
        self._restore_chat()
        self._refresh_chrome()

    def _refresh_hint_bar(self) -> None:
        """The keys that matter right now, from the live keymap."""
        bar = self.query_one("#hint-bar", Static)
        parts = []
        for action in ("retry_turn", "toggle_follow", "op_next", "op_close"):
            key = self.keymap.get(action) or ""
            if key:
                label_key = "keys.action." + action.replace("_", "-")
                parts.append(f"{key} {self.translator.t(label_key)}")
        bar.update(" · ".join(parts))

    def _refresh_chrome(self) -> None:
        self._refresh_hint_bar()
        self._refresh_attack_map()
        self._refresh_operations_strip()
        self._refresh_killchain()
        self._refresh_welcome()
        self.query_one("#brand", Static).update(self._brand_text())
        self.query_one("#tab-chat", Tab).label = self._chat_label()
        workspace = Text.assemble(
            (self.translator.t("side.workspace"), self.paint("dim text-faint")),
            ("\n" + self._short_cwd(), self.paint("accent")),
        )
        self.query_one("#side-workspace", Static).update(workspace)
        self._refresh_swarm_panel()
        self._refresh_side_blocks()
        model = self.models.default_model()
        directory = self._short_cwd()
        if not self._directory_trusted():
            directory += self.translator.t("trust.untrusted-marker")
        bar = Text(f"{directory} | ")
        # Green dot: ask before running OS commands; red dot: never ask.
        mode = self._permission_mode
        bar.append(
            "●",
            style=self.paint(
                {
                    PermissionMode.ASK: "success",
                    PermissionMode.YOLO: "risk-mid",
                    PermissionMode.NEVER: "danger",
                }[mode]
            ),
        )
        bar.append(f" {mode.value}")
        if self._plan_mode:
            bar.append(" | plan", style=self.paint("$accent"))
        if model:
            reasoning = self.models.default_reasoning_effort() or "off"
            bar.append(f" | model={model} reasoning={reasoning}")
        bar.append(f" | ctx {self._ctx_tokens} tok")
        limit = self._context_limit()
        if limit:
            ratio = self._ctx_tokens / limit
            token = "success" if ratio < 0.6 else ("risk-mid" if ratio < 0.85 else "danger")
            bar.append(
                f" {self._ctx_tokens // 1000}k/{limit // 1000}k",
                style=self.paint(token),
            )
        bar.append(f" | Σ {self._run_tokens} tok")
        if getattr(self, "_run_cost", 0.0):
            bar.append(f" | ${self._run_cost:.4f}")
        if self._turn_started is not None:
            elapsed = int(time.monotonic() - self._turn_started)
            bar.append(f" | ⏱ {elapsed // 60}:{elapsed % 60:02d}", style=self.paint("$accent"))
            if self._current_tool:
                bar.append(
                    f" | ▶ {self._current_tool[:32]}", style=self.paint("dim $text-dim")
                )
        bar.append(" | /help")
        self.query_one("#status-bar", Static).update(bar)
        self._refresh_ops_strip()
        self._refresh_hunt_tab()
        switcher = self.query_one("#views", ContentSwitcher)
        current = str(switcher.current or "journal")
        if current != "journal":
            self._render_view(current)

    def _status_tick(self) -> None:
        """While a turn runs the bar shows elapsed time and the current tool."""
        if self._turn_started is not None:
            self._refresh_chrome()

    def _refresh_hunt_tab(self) -> None:
        """The progress tab exists for every operation: findings and hypotheses
        live there even when no brute-force/fuzz task is in the plan."""
        tab = self.query_one("#tab-hunt", Tab)
        tab.display = self.operation is not None

    def _refresh_goal_strip(self, item: Static) -> None:
        goal = self._current_goal()
        if goal is None:
            item.display = False
            return
        item.update(
            Text(
                self.translator.t(
                    f"goal.strip.{goal.state.value}",
                    steps=f"{goal.spent_steps}/{goal.budget_steps}",
                    seconds=f"{goal.spent_seconds}/{goal.budget_seconds}",
                ),
                style=self.paint("$accent"),
            )
        )
        item.tooltip = goal.objective
        item.display = True

    def _current_goal(self) -> Goal | None:
        if self.operation is None:
            return None
        return self.goals.current(self.operation.id)

    def _agent_now_text(self, task_id: str) -> str:
        """What the active agent does right now: its latest tool in flight."""
        if self.operation is None:
            return ""
        runs = [
            run
            for run in self.execution.list_runs(self.operation.id)
            if run.task_id == task_id and run.status.value in ("running", "queued")
        ]
        if not runs:
            return ""
        return str(self.translator.t("strip.agent-now", tool=runs[-1].tool_id))

    def _context_limit(self) -> int:
        """The active model's context window, or 0 when unknown."""
        model_id = self.models.default_model()
        if not model_id:
            return 0
        try:
            model, _ = self.models.resolve_model(model_id)
        except Exception:  # noqa: BLE001 - an unknown model has no gauge
            return 0
        return model.context_limit

    def _refresh_ops_strip(self) -> None:
        agents_item = self.query_one("#strip-agents", Static)
        todo_item = self.query_one("#strip-todo", Static)
        task_item = self.query_one("#strip-task", Static)
        goal_item = self.query_one("#strip-goal", Static)
        if not self.operation:
            for item in (agents_item, todo_item, task_item, goal_item):
                item.display = False
            return
        self._refresh_goal_strip(goal_item)
        operation_id = self.operation.id
        runs = self.agents.list_runs(operation_id)
        if runs:
            active = self._active_spawn(operation_id)
            running = sum(1 for run in runs if run.status == AgentRunStatus.RUNNING)
            if active is not None:
                agents_item.update(
                    Text(
                        self.translator.t("strip.subagent", run=active.run_id, task=active.task_id)
                        + self._agent_now_text(active.task_id),
                        style=self.paint("$accent-alt"),
                    )
                )
            else:
                agents_item.update(
                    Text(
                        self.translator.t("strip.agents", running=running, total=len(runs)),
                        style=self.paint("$accent-alt"),
                    )
                )
            agents_item.tooltip = "\n".join(
                f"{run.id} | {run.task_id} | {run.status.value}" for run in runs[-8:]
            )
            agents_item.display = True
        else:
            agents_item.display = False
        checklist = getattr(self, "_turn_todo", [])
        if checklist:
            done_steps = sum(1 for _, status in checklist if status == "done")
            current_step = next(
                (title for title, status in checklist if status == "in_progress"),
                next((title for title, status in checklist if status != "done"), ""),
            )
            todo_item.update(
                Text(
                    self.translator.t(
                        "strip.checklist", done=done_steps, total=len(checklist)
                    )
                    + (f" · {current_step}" if current_step else ""),
                    style=self.paint("$accent"),
                )
            )
            todo_item.add_class("strip-checklist")
            todo_item.tooltip = "\n".join(f"{s} | {title}" for title, s in checklist)
            todo_item.display = True
        plan = self.kernel.get_current_plan(operation_id)
        tasks = plan.tasks if plan is not None else []
        if not tasks:
            todo_item.display = False
            task_item.display = False
            return
        if not checklist:
            done = sum(1 for task in tasks if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED))
            todo_item.update(
                Text(
                    self.translator.t("strip.todo", done=done, total=len(tasks)),
                    style=self.paint("$accent"),
                )
            )
            todo_item.remove_class("strip-checklist")
            todo_item.tooltip = "\n".join(
                f"{task.id} | {task.status.value} | {task.title}" for task in tasks
            )
            todo_item.display = True
        current = next((task for task in tasks if task.status == TaskStatus.IN_PROGRESS), None)
        if current is not None:
            task_item.update(Text(f"▸ {current.title}", style=self.paint("$warning")))
            task_item.tooltip = f"{current.id} | risk={current.risk_class.value}"
            task_item.display = True
        else:
            task_item.display = False

    def _show_settings(self) -> None:
        """Provider keys for tools: stored once here, injected by env name."""
        from samurai_ai.services.tool_keys import CATALOGUE, key_state, set_key

        states = {
            entry.env_name: key_state(self.vault, entry.env_name) for entry in CATALOGUE
        }
        self.push_screen(
            SettingsScreen(
                self.translator,
                states,
                lambda env_name, value: set_key(
                    self.vault, env_name, value, by=self._actor()
                ),
                lambda env_name: set_key(self.vault, env_name, ""),
                max_tool_rounds=self._max_tool_rounds(),
                on_save_limit=self._save_max_tool_rounds,
            )
        )
        self._write_line("[settings] " + self.translator.t("settings.opened"))

    def _save_max_tool_rounds(self, raw: str) -> None:
        """The rounds ceiling from /settings; a bad value is a note, not a crash."""
        try:
            value = int(raw)
        except ValueError:
            self._write_dim(self.translator.t("settings.rounds-bad", value=raw))
            return
        self.settings.set_value("tui.max_tool_rounds", str(value))
        self._write_dim(
            self.translator.t("settings.rounds-set", value=self._max_tool_rounds())
        )

    def _provider_oauth(self, provider_id: str) -> None:
        """`/oauth <provider>`: device-code login, token lands in the vault."""
        provider_id = provider_id.strip()
        if not provider_id:
            self._write_dim(self.translator.t("oauth.usage"))
            return
        issuer = self.settings.get_value(f"model.oauth.{provider_id}.issuer") or ""
        client_id = self.settings.get_value(f"model.oauth.{provider_id}.client_id") or ""
        if not issuer or not client_id:
            self._write_dim(
                self.translator.t("oauth.not-configured", provider=provider_id)
            )
            return
        self.run_worker(self._run_oauth(provider_id, issuer, client_id), exclusive=False)

    async def _run_oauth(self, provider_id: str, issuer: str, client_id: str) -> None:
        from samurai_ai.services import provider_oauth

        def transport(url: str, form: dict[str, str]) -> tuple[int, dict[str, object]]:
            import json as _json
            import urllib.parse
            import urllib.request

            body = urllib.parse.urlencode(form).encode()
            request = urllib.request.Request(
                url, data=body, headers={"Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, _json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                return exc.code, _json.loads(exc.read() or b"{}")

        prompt = await asyncio.to_thread(provider_oauth.begin, transport, issuer, client_id)
        self._write_line(self.translator.t("oauth.open", url=prompt.verification_url))
        self._write_line(self.translator.t("oauth.code", code=prompt.user_code))
        try:
            token = await asyncio.to_thread(
                provider_oauth.wait_for_token, transport, issuer, client_id, prompt
            )
        except provider_oauth.DeviceFlowError as exc:
            self._write_line(f"[oauth] {exc}")
            return
        self.vault.add(f"{provider_id}-access-token", token, created_by=self._actor())
        self._write_dim(self.translator.t("oauth.saved", provider=provider_id))
        self._refresh_chrome()

    def _show_model_picker(self) -> None:
        self.run_worker(self._open_model_picker(), exclusive=True)

    async def _open_model_picker(self) -> None:
        providers = self.models.list_providers()
        configured = {
            provider["id"]
            for provider in providers
            if provider["local"] or provider["auth"] in ("configured", "none")
        }
        model_ids: list[str] = [
            model.id for model in self.models.list_models() if model.provider_id in configured
        ]
        for provider in providers:
            if provider["id"] not in configured:
                continue
            try:
                discovered = await asyncio.to_thread(self.models.discover_models, provider["id"])
                for model in discovered:
                    if model.id not in model_ids:
                        model_ids.append(model.id)
            except Exception:  # noqa: BLE001 - discovery is best-effort
                continue
        current = self.models.default_model()
        if current and current not in model_ids:
            model_ids.append(current)
        reasoning_by_model = {
            model_id: stored
            for model_id in model_ids
            if (stored := self.settings.get_value(f"model.reasoning_effort.{model_id}"))
            in REASONING_LEVELS
        }
        reasoning = (
            reasoning_by_model.get(current or "") or self.models.default_reasoning_effort() or "off"
        )
        # providers without a key stay visible: the operator can set it right here
        needs_key = [
            (provider["id"], provider["id"])
            for provider in providers
            if provider["auth"] == "missing" and provider["secret_name"]
        ]
        self._key_secret_by_provider = {
            provider["id"]: provider["secret_name"]
            for provider in providers
            if provider["secret_name"]
        }
        self.push_screen(
            ModelPickerScreen(
                self.translator,
                model_ids,
                current,
                reasoning,
                add_custom_label=self.translator.t("model.picker-add-custom"),
                current_marker=self.translator.t("model.current-marker"),
                reasoning_by_model=reasoning_by_model,
                needs_key=needs_key,
                needs_key_label=self.translator.t("model.needs-key"),
            ),
            self._on_model_chosen,
        )

    def _on_model_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        model_id, reasoning = choice
        if model_id.startswith("key:"):
            self.run_worker(self._ask_provider_key(model_id[4:]), exclusive=False)
            return
        if model_id == ADD_CUSTOM_ID:
            self.push_screen(
                CustomEndpointScreen(self.translator, self.models, self.vault, self.settings),
                self._on_custom_endpoint_done,
            )
            return
        self.settings.set_value("model.default", model_id)
        self.settings.set_value("model.reasoning_effort", reasoning)
        self.settings.set_value(f"model.reasoning_effort.{model_id}", reasoning)
        self._write_line(
            "[model] " + self.translator.t("model.selected", model=model_id, reasoning=reasoning)
        )
        self._refresh_chrome()
        self.query_one("#command-input", Input).focus()

    async def _ask_provider_key(self, provider_id: str) -> None:
        """Take the provider's key once, store it masked, then reopen /model."""
        secret = getattr(self, "_key_secret_by_provider", {}).get(provider_id)
        if secret is None:
            return
        answer = await self.push_screen_wait(
            ArgumentScreen(
                self.translator,
                self.translator.t("model.key-prompt", provider=provider_id),
            )
        )
        if not answer:
            return
        self.vault.add(secret, answer.strip(), created_by=self._actor())
        self._write_dim(self.translator.t("model.key-saved", provider=provider_id))
        self._refresh_chrome()
        await self._open_model_picker()

    def _on_custom_endpoint_done(self, model_id: str | None) -> None:
        if model_id:
            reasoning = self.models.default_reasoning_effort() or "off"
            self._write_line(
                "[model] "
                + self.translator.t("model.selected", model=model_id, reasoning=reasoning)
            )
            self._refresh_chrome()
        self.query_one("#command-input", Input).focus()


def _first_code_block(text: str) -> str | None:
    match = re.search(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    if match is None:
        return None
    return match.group(1).rstrip("\n")


def _artifact_links(artifact: Artifact) -> str:
    links: list[str] = []
    if artifact.task_id:
        links.append(f"task={artifact.task_id}")
    if artifact.gate_id:
        links.append(f"gate={artifact.gate_id}")
    if artifact.finding_id:
        links.append(f"finding={artifact.finding_id}")
    return ",".join(links) or "-"
