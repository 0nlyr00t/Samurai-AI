import asyncio
import json
import re
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from samurai_ai.domain.kernel import Artifact, GateStatus, TaskStatus
from samurai_ai.domain.operations import Operation
from samurai_ai.i18n import Translator
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.memory import CampaignMemoryService
from samurai_ai.services.models import ModelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.services.skills import SkillService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.models import CustomModelConflictError
from samurai_ai.storage.swarm import SwarmRunRepository


class LanguagePromptScreen(ModalScreen[str]):
    CSS = """
    LanguagePromptScreen {
        align: center middle;
    }
    #language-dialog {
        width: 52;
        height: auto;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #language-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    #language-options {
        height: auto;
        background: #0a0a0a;
    }
    """

    def __init__(self, translator: Translator) -> None:
        super().__init__()
        self.translator = translator

    def compose(self) -> ComposeResult:
        with Vertical(id="language-dialog"):
            yield Static(self.translator.t("chooser.title"), id="language-title")
            yield OptionList(
                Option(self.translator.t("chooser.english"), id="en"),
                Option(self.translator.t("chooser.russian"), id="ru"),
                id="language-options",
            )

    def on_mount(self) -> None:
        self.query_one("#language-options", OptionList).focus()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(str(event.option.id or "en"))


REASONING_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")

ADD_CUSTOM_ID = "__add_custom__"


class ModelPickerScreen(ModalScreen[tuple[str, str]]):
    """Arrow-key model and reasoning picker, in the spirit of opencode."""

    CSS = """
    ModelPickerScreen {
        align: center middle;
    }
    #model-dialog {
        width: 84;
        height: auto;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #model-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    #model-columns {
        height: 14;
    }
    .picker-column {
        width: 1fr;
        height: 100%;
    }
    .picker-label {
        color: #888888;
        padding-bottom: 1;
    }
    #model-hint {
        color: #888888;
        padding-top: 1;
    }
    """

    def __init__(
        self,
        translator: Translator,
        model_ids: list[str],
        current_model: str | None,
        current_reasoning: str,
        add_custom_label: str | None = None,
        current_marker: str = "",
        reasoning_by_model: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.model_ids = model_ids
        self.add_custom_label = add_custom_label
        self.current_marker = current_marker
        self.reasoning_by_model = reasoning_by_model or {}
        self.current_model = current_model
        self.current_reasoning = current_reasoning
        self.selected_model = current_model or (
            model_ids[0] if model_ids else ADD_CUSTOM_ID
        )
        self.selected_reasoning = current_reasoning

    def compose(self) -> ComposeResult:
        model_options: list[Option] = []
        if self.add_custom_label is not None:
            model_options.append(Option(self.add_custom_label, id="m-add"))
        for index, model_id in enumerate(self.model_ids):
            label = model_id
            if self.current_marker and model_id == self.current_model:
                label = f"{model_id}{self.current_marker}"
            model_options.append(Option(label, id=f"m{index}"))
        with Vertical(id="model-dialog"):
            yield Static(self.translator.t("model.picker-title"), id="model-title")
            with Horizontal(id="model-columns"):
                with Vertical(classes="picker-column"):
                    yield Static(
                        self.translator.t("model.picker-models"), classes="picker-label"
                    )
                    yield OptionList(*model_options, id="model-options")
                with Vertical(classes="picker-column"):
                    yield Static(
                        self.translator.t("model.picker-reasoning"),
                        classes="picker-label",
                    )
                    yield OptionList(
                        *[
                            Option(level, id=f"r-{level}")
                            for level in REASONING_LEVELS
                        ],
                        id="reasoning-options",
                    )
            yield Static(self.translator.t("model.picker-hint"), id="model-hint")

    def on_mount(self) -> None:
        models = self.query_one("#model-options", OptionList)
        if self.current_model in self.model_ids:
            offset = 1 if self.add_custom_label is not None else 0
            models.highlighted = self.model_ids.index(self.current_model) + offset
        reasoning = self.query_one("#reasoning-options", OptionList)
        reasoning.highlighted = REASONING_LEVELS.index(self.current_reasoning)
        models.focus()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "model-options" and event.option is not None:
            index = event.option_list.highlighted
            if index is None:
                return
            if self.add_custom_label is not None:
                if index == 0:
                    self.selected_model = ADD_CUSTOM_ID
                    return
                index -= 1
            if 0 <= index < len(self.model_ids):
                model_id = self.model_ids[index]
                changed = model_id != self.selected_model
                self.selected_model = model_id
                # Follow the model's own stored reasoning level unless the
                # operator is actively choosing reasoning right now.
                reasoning_list = self.query_one("#reasoning-options", OptionList)
                stored = self.reasoning_by_model.get(model_id)
                if (
                    changed
                    and stored in REASONING_LEVELS
                    and not reasoning_list.has_focus
                ):
                    self.selected_reasoning = stored
                    reasoning_list.highlighted = REASONING_LEVELS.index(stored)
        elif event.option_list.id == "reasoning-options" and event.option is not None:
            index = event.option_list.highlighted
            if index is not None:
                self.selected_reasoning = REASONING_LEVELS[index]

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if (
            event.option_list.id == "model-options"
            and event.option is not None
            and event.option.id == "m-add"
        ):
            self.dismiss((ADD_CUSTOM_ID, self.selected_reasoning))
            return
        self.dismiss((self.selected_model, self.selected_reasoning))

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)


class CustomEndpointScreen(ModalScreen[str | None]):
    """Interactive wizard for adding a custom OpenAI-compatible endpoint.

    Endpoint id, base URL, API key (stored encrypted in the vault), a TLS
    verification toggle, then live model discovery with arrow-key selection
    or manual entry. Dismisses with the chosen default model id (or None).
    """

    CSS = """
    CustomEndpointScreen {
        align: center middle;
    }
    #ce-dialog {
        width: 78;
        height: auto;
        padding: 1 2;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #ce-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    .ce-label {
        color: #888888;
        padding-top: 1;
    }
    #ce-tls {
        height: auto;
        background: #0a0a0a;
    }
    #ce-fetch {
        margin-top: 1;
    }
    #ce-status {
        color: #ffd43b;
        padding-top: 1;
    }
    #ce-models {
        height: 8;
        background: #0a0a0a;
        display: none;
    }
    #ce-manual {
        display: none;
    }
    """

    def __init__(
        self,
        translator: Translator,
        models: ModelService,
        vault: VaultService,
        settings: SettingsService,
    ) -> None:
        super().__init__()
        self.translator = translator
        self.models = models
        self.vault = vault
        self.settings = settings
        self._endpoint_id: str | None = None
        self._model_names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="ce-dialog"):
            yield Static(self.translator.t("model.custom-title"), id="ce-title")
            yield Input(placeholder=self.translator.t("model.custom-id"), id="ce-id")
            yield Input(placeholder=self.translator.t("model.custom-url"), id="ce-url")
            yield Input(
                placeholder=self.translator.t("model.custom-key"),
                id="ce-key",
                password=True,
            )
            yield Static(self.translator.t("model.custom-tls"), classes="ce-label")
            yield OptionList(
                Option(self.translator.t("model.custom-tls-on"), id="tls-on"),
                Option(self.translator.t("model.custom-tls-off"), id="tls-off"),
                id="ce-tls",
            )
            yield Button(self.translator.t("model.custom-fetch"), id="ce-fetch")
            yield Static("", id="ce-status")
            yield OptionList(id="ce-models")
            yield Input(
                placeholder=self.translator.t("model.custom-manual"), id="ce-manual"
            )

    def on_mount(self) -> None:
        self.query_one("#ce-tls", OptionList).highlighted = 0
        self.query_one("#ce-id", Input).focus()

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key == "escape":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ce-manual":
            self._finalize(event.value.strip())
        elif event.input.id in ("ce-id", "ce-url", "ce-key"):
            self._connect()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ce-fetch":
            self._connect()

    def _connect(self) -> None:
        status = self.query_one("#ce-status", Static)
        endpoint_id = self.query_one("#ce-id", Input).value.strip()
        base_url = self.query_one("#ce-url", Input).value.strip()
        api_key = self.query_one("#ce-key", Input).value.strip()
        tls = self.query_one("#ce-tls", OptionList)
        verify_tls = tls.highlighted in (None, 0)
        if not endpoint_id or not base_url:
            status.update(self.translator.t("model.custom-invalid"))
            return
        if self.models.endpoints is None:
            status.update(
                self.translator.t(
                    "model.custom-failed", error="endpoint persistence not configured"
                )
            )
            return
        if any(
            provider.id == endpoint_id
            for provider in self.models.registry.static_providers
        ):
            status.update(
                self.translator.t(
                    "model.custom-failed", error=f"id {endpoint_id!r} is reserved"
                )
            )
            return
        try:
            if self.models.endpoints.get(endpoint_id) is not None:
                self.models.remove_endpoint(endpoint_id)
            secret_name = None
            if api_key:
                secret_name = f"{endpoint_id}-api-key"
                existing = {entry.name for entry in self.vault.list_metadata()}
                if secret_name in existing:
                    self.vault.remove(secret_name)
                self.vault.add(secret_name, api_key)
            self.models.register_endpoint(
                endpoint_id, base_url, secret_name, verify_tls=verify_tls
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
            return
        self._endpoint_id = endpoint_id
        self.query_one("#ce-manual", Input).display = True
        self.run_worker(self._discover(endpoint_id), exclusive=True)

    async def _discover(self, endpoint_id: str) -> None:
        status = self.query_one("#ce-status", Static)
        button = self.query_one("#ce-fetch", Button)
        button.disabled = True
        status.update(self.translator.t("model.custom-fetching"))
        try:
            discovered = await asyncio.to_thread(
                self.models.discover_models, endpoint_id
            )
            self._model_names = [entry.model_name for entry in discovered]
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            self._model_names = []
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
        finally:
            button.disabled = False
        options = self.query_one("#ce-models", OptionList)
        options.clear_options()
        if self._model_names:
            options.add_options(
                Option(name, id=f"c{index}")
                for index, name in enumerate(self._model_names)
            )
            options.display = True
            options.highlighted = 0
            status.update(self.translator.t("model.custom-pick"))
            options.focus()
        else:
            options.display = False
            if self._endpoint_id is not None:
                status.update(self.translator.t("model.custom-no-models"))

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        if event.option_list.id != "ce-models" or event.option is None:
            return
        index = int(str(event.option.id or "c0")[1:])
        if 0 <= index < len(self._model_names):
            self._finalize(self._model_names[index])

    def _finalize(self, model_name: str) -> None:
        if not model_name or self._endpoint_id is None:
            return
        status = self.query_one("#ce-status", Static)
        try:
            model = self.models.add_model(f"{self._endpoint_id}/{model_name}")
        except CustomModelConflictError:
            model, _ = self.models.resolve_model(
                f"{self._endpoint_id}/{model_name}@v1"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            status.update(self.translator.t("model.custom-failed", error=str(exc)))
            return
        self.settings.set_value("model.default", model.id)
        self.dismiss(model.id)


class HistoryInput(Input):
    """Command input with shell-like history on the Up/Down arrows."""

    BINDINGS = [
        Binding("up", "history_previous", show=False),
        Binding("down", "history_next", show=False),
    ]

    def action_history_previous(self) -> None:
        step = getattr(self.app, "history_step", None)
        if step is not None:
            step(-1)

    def action_history_next(self) -> None:
        step = getattr(self.app, "history_step", None)
        if step is not None:
            step(1)


class SamuraiApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        background: #000000;
        color: #e0e0e0;
    }
    #operation-header {
        height: 1;
        padding: 0 1;
        background: #0a0a0a;
        color: #4fa8ff;
        text-style: bold;
    }
    #journal {
        height: 1fr;
        padding: 0 1;
        border: solid #3a3a3a;
        background: #000000;
    }
    #command-input {
        height: 3;
        border: solid #5bc0be;
        background: #0a0a0a;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        background: #0a0a0a;
        color: #888888;
    }
    """

    _TAG_STYLES = {
        "error": "bold #ff6b6b",
        "deny": "#ff6b6b",
        "allow": "#51cf66",
        "status": "#4fa8ff",
        "scope": "#4fa8ff",
        "plan": "#74c0fc",
        "task": "#74c0fc",
        "gate": "#ffd43b",
        "approval": "#ffd43b",
        "finding": "#da77f2",
        "evidence": "#ced4da",
        "artifact": "#ced4da",
        "snapshot": "#ced4da",
        "tools": "#63e6be",
        "tool": "#63e6be",
        "run": "#63e6be",
        "runs": "#63e6be",
        "agent": "#5bc0be",
        "agents": "#5bc0be",
        "swarm": "#5bc0be",
        "member": "#5bc0be",
        "memory": "#b197fc",
        "skill": "#b197fc",
        "skills": "#b197fc",
        "playbook": "#b197fc",
        "playbooks": "#b197fc",
        "secret": "#ffa94d",
        "secrets": "#ffa94d",
        "asset": "#a9e34b",
        "assets": "#a9e34b",
        "report": "#ffa94d",
        "model": "#4fa8ff",
        "info": "#888888",
    }

    BINDINGS = [
        Binding("ctrl+c", "quit_requested", "Quit", show=False),
        Binding("ctrl+o", "toggle_journal", "Toggle journal", show=False),
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
        playbooks: PlaybookService,
        reports: ReportService,
        ctf: CtfService,
        redteam: RedTeamService,
        planning: PlanningService,
        swarms: SwarmRunRepository,
        memory: CampaignMemoryService,
        skills: SkillService,
        models: ModelService,
        operation_id: str | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.kernel = kernel
        self.settings = settings
        self.recovery = recovery
        self.execution = execution
        self.vault = vault
        self.agents = agents
        self.playbooks = playbooks
        self.reports = reports
        self.ctf = ctf
        self.redteam = redteam
        self.planning = planning
        self.swarms = swarms
        self.memory = memory
        self.skills = skills
        self.models = models
        self.requested_operation_id = operation_id
        self.operation: Operation | None = None
        self.translator = Translator(settings.get_locale() or "en")
        self._locale_known = settings.get_locale() is not None
        self._last_quit_request = 0.0
        self._history: list[str] = json.loads(settings.get_value("tui.history") or "[]")
        self._history_index: int | None = None
        self._history_draft = ""

    def _write_line(self, text: str) -> None:
        journal = self.query_one("#journal", RichLog)
        match = re.match(r"^\[([a-z-]+)\]", text)
        style = self._TAG_STYLES.get(match.group(1), "") if match else ""
        journal.write(Text(text, style=style))

    def compose(self) -> ComposeResult:
        yield Static(id="operation-header")
        yield RichLog(id="journal", markup=False, wrap=True, auto_scroll=True)
        yield HistoryInput(
            placeholder=self.translator.t("input.placeholder"), id="command-input"
        )
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        try:
            if self.requested_operation_id:
                self.operation = self.service.get(self.requested_operation_id)
            else:
                self.operation = self.service.latest()
        except OperationNotFoundError as exc:
            self._write_line(f"[error] {exc}")

        self._refresh_chrome()
        if self._locale_known:
            self._write_welcome()
            self.query_one("#command-input", Input).focus()
        else:
            self.push_screen(LanguagePromptScreen(self.translator), self._on_locale_chosen)

    def _on_locale_chosen(self, locale: str | None) -> None:
        if locale:
            self.settings.set_locale(locale)
            self.translator.set_locale(locale)
            self._locale_known = True
        self.query_one("#command-input", Input).placeholder = self.translator.t(
            "input.placeholder"
        )
        self._write_welcome()
        self._refresh_chrome()
        self.query_one("#command-input", Input).focus()

    def _write_welcome(self) -> None:
        self._write_line(self.translator.t("welcome.title"))
        self._write_line(self.translator.t("welcome.stage"))
        if self.operation:
            self._write_line(
                self.translator.t(
                    "welcome.loaded", operation_id=self.operation.id, name=self.operation.name
                )
            )
        else:
            self._write_line(self.translator.t("welcome.no-operation"))
        self._write_line(self.translator.t("welcome.help-hint"))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        if command:
            if not self._history or self._history[-1] != command:
                self._history.append(command)
                self._history = self._history[-200:]
                self.settings.set_value("tui.history", json.dumps(self._history))
            self._history_index = None
            self._history_draft = ""
            self._handle_command(command)

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

    def action_quit_requested(self) -> None:
        now = time.monotonic()
        if now - self._last_quit_request <= self._QUIT_CONFIRM_SECONDS:
            self.exit()
            return
        self._last_quit_request = now
        self._write_line("[info] " + self.translator.t("app.quit-hint"))

    def action_toggle_journal(self) -> None:
        journal = self.query_one("#journal", RichLog)
        journal.display = not journal.display
        self.query_one("#command-input", Input).focus()

    def _handle_command(self, command: str) -> None:
        name, _, argument = command.partition(" ")

        if name in ("/exit", "/quit"):
            self.exit()
            return
        if name == "/help":
            for line in self.translator.help_lines():
                self._write_line(line)
            return
        if name == "/status":
            self._show_status()
            return
        if name == "/scope":
            self._show_scope(argument.strip())
            return
        if name == "/plan":
            self._show_plan()
            return
        if name == "/tasks":
            self._show_tasks()
            return
        if name == "/gates":
            self._show_gates()
            return
        if name == "/findings":
            self._show_findings()
            return
        if name == "/evidence":
            self._show_evidence()
            return
        if name == "/approvals":
            self._show_approvals()
            return
        if name == "/snapshots":
            self._show_snapshots()
            return
        if name == "/tools":
            self._show_tools()
            return
        if name == "/runs":
            self._show_runs()
            return
        if name == "/agents":
            self._show_agents()
            return
        if name == "/swarm":
            self._show_swarms()
            return
        if name == "/memory":
            self._show_memory(argument.strip())
            return
        if name == "/skills":
            self._show_skills()
            return
        if name == "/playbooks":
            self._show_custom_playbooks()
            return
        if name == "/secrets":
            self._show_secrets()
            return
        if name == "/assets":
            self._show_assets()
            return
        if name == "/report":
            self._show_reports()
            return
        if name == "/hypotheses":
            self._show_hypotheses()
            return
        if name == "/flags":
            self._show_flags()
            return
        if name == "/attack-path":
            self._show_attack_path()
            return
        if name == "/debrief":
            self._show_debriefs()
            return
        if name == "/test-cases":
            self._show_test_cases()
            return
        if name == "/runbook":
            self._show_runbooks()
            return
        if name == "/reconcile":
            self._show_reconcile()
            return
        if name == "/lang":
            self._show_lang(argument.strip())
            return
        if name == "/model":
            self._show_model_picker()
            return

        if name.startswith("/"):
            self._write_line(
                "[error] " + self.translator.t("error.unknown-command", name=name)
            )
        else:
            self._write_line("[info] " + self.translator.t("info.ai-disabled"))

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
                "[scope] "
                + self.translator.t("scope.includes", rules=", ".join(scope.includes))
            )
            excludes = ", ".join(scope.excludes) or self.translator.t("scope.none")
            self._write_line(
                "[scope] " + self.translator.t("scope.excludes", rules=excludes)
            )
            return
        decision = scope.evaluate(target)
        level = "allow" if decision.allowed else "deny"
        self._write_line(
            f"[{level}] {decision.target}: {decision.reason.value} — {decision.detail}"
        )

    def _show_plan(self) -> None:
        if not self.operation:
            self._write_line("[plan] " + self.translator.t("status.no-operation"))
            return
        operation = self.operation
        objective = operation.objective or self.translator.t("plan.objective-unset")
        plan = self.kernel.get_current_plan(operation.id)
        self._write_line(
            "[plan] "
            + self.translator.t(
                "plan.stage", stage=operation.status.value, objective=objective
            )
        )
        if plan is None:
            self._write_line("[plan] " + self.translator.t("plan.no-plan"))
            return
        approved = self.translator.t(
            "plan.approved" if plan.approved else "plan.not-approved"
        )
        tasks_done = sum(
            1 for task in plan.tasks if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
        )
        gates_passed = sum(1 for gate in plan.gates if gate.status == GateStatus.PASSED)
        self._write_line(
            f"[plan] {plan.id} | {plan.title} | {approved} | version={plan.version} | "
            f"tasks={tasks_done}/{len(plan.tasks)} | gates={gates_passed}/{len(plan.gates)}"
        )

    def _show_tasks(self) -> None:
        if not self.operation:
            self._write_line("[tasks] " + self.translator.t("status.no-operation"))
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.tasks:
            self._write_line("[tasks] " + self.translator.t("tasks.empty"))
            return
        for task in plan.tasks:
            dependencies = ",".join(task.depends_on) or "-"
            requirements = ",".join(task.required_artifacts) or "-"
            self._write_line(
                f"[task] {task.id} | {task.status.value} | risk={task.risk_class.value} | "
                f"deps={dependencies} | artifacts={requirements} | {task.title}"
            )

    def _show_gates(self) -> None:
        if not self.operation:
            self._write_line("[gates] " + self.translator.t("status.no-operation"))
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.gates:
            self._write_line("[gates] " + self.translator.t("gates.empty"))
            return
        for gate in plan.gates:
            requirements = ",".join(gate.required_artifacts) or "-"
            self._write_line(
                f"[gate] {gate.id} | {gate.status.value} | artifacts={requirements} | {gate.name}"
            )

    def _show_findings(self) -> None:
        if not self.operation:
            self._write_line("[findings] " + self.translator.t("status.no-operation"))
            return
        findings = self.kernel.kernel.list_findings(self.operation.id)
        if not findings:
            self._write_line("[findings] " + self.translator.t("findings.empty"))
            return
        for finding in findings:
            duplicate = f" | duplicate-of={finding.duplicate_of}" if finding.duplicate_of else ""
            self._write_line(
                f"[finding] {finding.id} | {finding.severity.value} | {finding.status.value} | "
                f"evidence={len(finding.evidence_ids)}{duplicate} | {finding.title}"
            )

    def _show_evidence(self) -> None:
        if not self.operation:
            self._write_line("[evidence] " + self.translator.t("status.no-operation"))
            return
        artifacts = self.kernel.kernel.list_artifacts(self.operation.id)
        if not artifacts:
            self._write_line("[evidence] " + self.translator.t("evidence.empty"))
            return
        for artifact in artifacts:
            self._write_line(
                f"[artifact] {artifact.id} | {artifact.kind.value} | "
                f"requirement={artifact.requirement or '-'} | "
                f"links={_artifact_links(artifact)} | sha256={artifact.sha256[:12]} | "
                f"{artifact.summary or artifact.path}"
            )

    def _show_approvals(self) -> None:
        if not self.operation:
            self._write_line("[approvals] " + self.translator.t("status.no-operation"))
            return
        approvals = self.kernel.kernel.list_approvals(self.operation.id)
        if not approvals:
            self._write_line("[approvals] " + self.translator.t("approvals.empty"))
            return
        for approval in approvals:
            actor = approval.decided_by or "-"
            self._write_line(
                f"[approval] {approval.id} | {approval.risk_class.value} | "
                f"{approval.status.value} | auto={str(approval.auto).lower()} | "
                f"actor={actor} | {approval.action}"
            )

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
            network = (
                self.translator.t("tools.network")
                if definition.network_access
                else self.translator.t("tools.no-network")
            )
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
            artifacts = ",".join(
                artifact_id
                for artifact_id in (run.stdout_artifact_id, run.stderr_artifact_id)
                if artifact_id
            ) or "-"
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

    def _show_memory(self, workspace: str) -> None:
        entries = self.memory.list_entries(workspace or None)
        if not entries:
            self._write_line("[memory] " + self.translator.t("memory.empty"))
            return
        for entry in entries:
            self._write_line(
                f"[memory] {entry.id} | {entry.workspace} | {entry.kind.value} | "
                f"{entry.title}"
            )

    def _show_skills(self) -> None:
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
            self._write_line(
                f"[hypothesis] {item.id} | {item.status.value} | {item.title}"
            )

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
                "[lang] "
                + self.translator.t("lang.current", locale=self.translator.locale)
            )
            return
        try:
            locale = self.settings.set_locale(argument)
        except InvalidLocaleError:
            self._write_line(
                "[error] " + self.translator.t("lang.invalid", locale=argument)
            )
            return
        self.translator.set_locale(locale)
        self.query_one("#command-input", Input).placeholder = self.translator.t(
            "input.placeholder"
        )
        self._write_line("[lang] " + self.translator.t("lang.changed", locale=locale))
        self._refresh_chrome()

    def _refresh_chrome(self) -> None:
        header = self.query_one("#operation-header", Static)
        status_bar = self.query_one("#status-bar", Static)
        if self.operation:
            operation = self.operation
            summary = self.kernel.summary(operation.id)
            header.update(
                self.translator.t(
                    "header.with-operation",
                    operation_id=operation.id,
                    mode=operation.mode.value.upper(),
                )
            )
            status_text = self.translator.t(
                "statusbar.with-operation",
                status=operation.status.value.upper(),
                tasks_done=summary.tasks_done,
                tasks_total=summary.tasks_total,
                gates_passed=summary.gates_passed,
                gates_total=summary.gates_total,
                pending=summary.pending_approvals,
            )
        else:
            header.update(self.translator.t("header.no-operation"))
            status_text = self.translator.t("statusbar.idle")
        model = self.models.default_model()
        if model:
            reasoning = self.models.default_reasoning_effort() or "off"
            status_text = f"{status_text} | model={model} reasoning={reasoning}"
        status_bar.update(status_text)

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
            model.id
            for model in self.models.list_models()
            if model.provider_id in configured
        ]
        for provider in providers:
            if provider["id"] not in configured:
                continue
            try:
                discovered = await asyncio.to_thread(
                    self.models.discover_models, provider["id"]
                )
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
            reasoning_by_model.get(current or "")
            or self.models.default_reasoning_effort()
            or "off"
        )
        self.push_screen(
            ModelPickerScreen(
                self.translator,
                model_ids,
                current,
                reasoning,
                add_custom_label=self.translator.t("model.picker-add-custom"),
                current_marker=self.translator.t("model.current-marker"),
                reasoning_by_model=reasoning_by_model,
            ),
            self._on_model_chosen,
        )

    def _on_model_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        model_id, reasoning = choice
        if model_id == ADD_CUSTOM_ID:
            self.push_screen(
                CustomEndpointScreen(
                    self.translator, self.models, self.vault, self.settings
                ),
                self._on_custom_endpoint_done,
            )
            return
        self.settings.set_value("model.default", model_id)
        self.settings.set_value("model.reasoning_effort", reasoning)
        self.settings.set_value(f"model.reasoning_effort.{model_id}", reasoning)
        self._write_line(
            "[model] "
            + self.translator.t("model.selected", model=model_id, reasoning=reasoning)
        )
        self._refresh_chrome()
        self.query_one("#command-input", Input).focus()

    def _on_custom_endpoint_done(self, model_id: str | None) -> None:
        if model_id:
            reasoning = self.models.default_reasoning_effort() or "off"
            self._write_line(
                "[model] "
                + self.translator.t(
                    "model.selected", model=model_id, reasoning=reasoning
                )
            )
            self._refresh_chrome()
        self.query_one("#command-input", Input).focus()


def _artifact_links(artifact: Artifact) -> str:
    links: list[str] = []
    if artifact.task_id:
        links.append(f"task={artifact.task_id}")
    if artifact.gate_id:
        links.append(f"gate={artifact.gate_id}")
    if artifact.finding_id:
        links.append(f"finding={artifact.finding_id}")
    return ",".join(links) or "-"
