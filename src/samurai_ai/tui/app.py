from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, RichLog, Static

from samurai_ai.domain.operations import Operation
from samurai_ai.services.operations import OperationNotFoundError, OperationService

_HELP_LINES = (
    "/help             Show available commands",
    "/status           Show the current operation",
    "/scope [target]   Show scope rules or evaluate a target",
    "/plan             Show the current objective and lifecycle stage",
    "/exit             Close Samurai AI",
    "Ctrl-O            Toggle the event journal",
    "Ctrl-C            Close Samurai AI",
)


class SamuraiApp(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
        color: #e0e0e0;
    }
    #operation-header {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #4fa8ff;
        text-style: bold;
    }
    #journal {
        height: 1fr;
        padding: 0 1;
        border: solid #5a5a5a;
    }
    #command-input {
        height: 3;
        border: solid #5bc0be;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #888888;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+o", "toggle_journal", "Toggle journal", show=False),
    ]

    def __init__(self, service: OperationService, operation_id: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.requested_operation_id = operation_id
        self.operation: Operation | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="operation-header")
        yield RichLog(id="journal", markup=False, wrap=True, auto_scroll=True)
        yield Input(
            placeholder="Enter a command; /help lists Phase 0 capabilities",
            id="command-input",
        )
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        journal = self.query_one("#journal", RichLog)
        try:
            if self.requested_operation_id:
                self.operation = self.service.get(self.requested_operation_id)
            else:
                self.operation = self.service.latest()
        except OperationNotFoundError as exc:
            journal.write(f"[error] {exc}")

        self._refresh_chrome()
        journal.write("Samurai AI 0.1.0 — governed operation shell")
        journal.write("Phase 0: operation state, scope policy, persistence, CLI and TUI")
        if self.operation:
            journal.write(f"Loaded {self.operation.id}: {self.operation.name}")
        else:
            journal.write("No operation loaded. Create one with: samurai operation create --help")
        journal.write("Use /help to list available commands.")
        self.query_one("#command-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        if command:
            self._handle_command(command)

    def action_toggle_journal(self) -> None:
        journal = self.query_one("#journal", RichLog)
        journal.display = not journal.display
        self.query_one("#command-input", Input).focus()

    def _handle_command(self, command: str) -> None:
        journal = self.query_one("#journal", RichLog)
        name, _, argument = command.partition(" ")

        if name in ("/exit", "/quit"):
            self.exit()
            return
        if name == "/help":
            for line in _HELP_LINES:
                journal.write(line)
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

        if name.startswith("/"):
            journal.write(f"[error] Unknown command: {name}. Use /help.")
        else:
            journal.write("[info] AI orchestration is not enabled in Phase 0. Use /help.")

    def _show_status(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[status] No operation loaded")
            return
        self.operation = self.service.get(self.operation.id)
        operation = self.operation
        journal.write(
            f"[status] {operation.id} | {operation.name} | {operation.mode.value} | "
            f"{operation.status.value} | version={operation.version}"
        )
        self._refresh_chrome()

    def _show_scope(self, target: str) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[scope] No operation loaded")
            return
        scope = self.operation.scope
        if not target:
            journal.write(f"[scope] includes: {', '.join(scope.includes)}")
            journal.write(f"[scope] excludes: {', '.join(scope.excludes) or '(none)'}")
            return
        decision = scope.evaluate(target)
        level = "allow" if decision.allowed else "deny"
        journal.write(
            f"[{level}] {decision.target}: {decision.reason.value} — {decision.detail}"
        )

    def _show_plan(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[plan] No operation loaded")
            return
        objective = self.operation.objective or "(objective not set)"
        journal.write(f"[plan] stage={self.operation.status.value}; objective={objective}")

    def _refresh_chrome(self) -> None:
        header = self.query_one("#operation-header", Static)
        status_bar = self.query_one("#status-bar", Static)
        if self.operation:
            operation = self.operation
            header.update(
                f"SAMURAI AI  {operation.id}  {operation.mode.value.upper()}  "
                "SCOPE: OK  MODEL: NOT CONFIGURED"
            )
            status_bar.update(
                f"{operation.status.value.upper()} | APPROVAL: ASK | R0 LOCAL | /help"
            )
        else:
            header.update("SAMURAI AI  NO OPERATION  SCOPE: NOT SET")
            status_bar.update("IDLE | APPROVAL: ASK | R0 LOCAL | /help")
