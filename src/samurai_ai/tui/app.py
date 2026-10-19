from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, RichLog, Static

from samurai_ai.domain.kernel import Artifact, GateStatus, TaskStatus
from samurai_ai.domain.operations import Operation
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService

_HELP_LINES = (
    "/help             Show available commands",
    "/status           Show the current operation and kernel summary",
    "/scope [target]   Show scope rules or evaluate a target",
    "/plan             Show the current plan and lifecycle stage",
    "/tasks            Show plan tasks",
    "/gates            Show stage gates",
    "/findings         Show findings",
    "/evidence         Show evidence artifacts",
    "/approvals        Show approval requests",
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

    def __init__(
        self,
        service: OperationService,
        kernel: KernelService,
        operation_id: str | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.kernel = kernel
        self.requested_operation_id = operation_id
        self.operation: Operation | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="operation-header")
        yield RichLog(id="journal", markup=False, wrap=True, auto_scroll=True)
        yield Input(
            placeholder="Enter a command; /help lists Operation Kernel capabilities",
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
        journal.write("Stage 1: plans, tasks, gates, approvals, evidence and findings")
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

        if name.startswith("/"):
            journal.write(f"[error] Unknown command: {name}. Use /help.")
        else:
            journal.write("[info] AI orchestration is not enabled yet. Use /help.")

    def _show_status(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[status] No operation loaded")
            return
        self.operation = self.service.get(self.operation.id)
        operation = self.operation
        summary = self.kernel.summary(operation.id)
        journal.write(
            f"[status] {operation.id} | {operation.name} | {operation.mode.value} | "
            f"{operation.status.value} | version={operation.version}"
        )
        journal.write(
            f"[kernel] tasks={summary.tasks_done}/{summary.tasks_total} | "
            f"gates={summary.gates_passed}/{summary.gates_total} | "
            f"pending-approvals={summary.pending_approvals} | "
            f"artifacts={summary.artifacts} | findings={summary.findings}"
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
        operation = self.operation
        objective = operation.objective or "(objective not set)"
        plan = self.kernel.get_current_plan(operation.id)
        journal.write(f"[plan] stage={operation.status.value}; objective={objective}")
        if plan is None:
            journal.write("[plan] No current plan. Create one with: samurai plan create")
            return
        approved = "approved" if plan.approved else "not-approved"
        tasks_done = sum(
            1 for task in plan.tasks if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
        )
        gates_passed = sum(1 for gate in plan.gates if gate.status == GateStatus.PASSED)
        journal.write(
            f"[plan] {plan.id} | {plan.title} | {approved} | version={plan.version} | "
            f"tasks={tasks_done}/{len(plan.tasks)} | gates={gates_passed}/{len(plan.gates)}"
        )

    def _show_tasks(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[tasks] No operation loaded")
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.tasks:
            journal.write("[tasks] No current plan tasks")
            return
        for task in plan.tasks:
            dependencies = ",".join(task.depends_on) or "-"
            requirements = ",".join(task.required_artifacts) or "-"
            journal.write(
                f"[task] {task.id} | {task.status.value} | risk={task.risk_class.value} | "
                f"deps={dependencies} | artifacts={requirements} | {task.title}"
            )

    def _show_gates(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[gates] No operation loaded")
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.gates:
            journal.write("[gates] No current plan gates")
            return
        for gate in plan.gates:
            requirements = ",".join(gate.required_artifacts) or "-"
            journal.write(
                f"[gate] {gate.id} | {gate.status.value} | artifacts={requirements} | {gate.name}"
            )

    def _show_findings(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[findings] No operation loaded")
            return
        findings = self.kernel.kernel.list_findings(self.operation.id)
        if not findings:
            journal.write("[findings] No findings")
            return
        for finding in findings:
            duplicate = f" | duplicate-of={finding.duplicate_of}" if finding.duplicate_of else ""
            journal.write(
                f"[finding] {finding.id} | {finding.severity.value} | {finding.status.value} | "
                f"evidence={len(finding.evidence_ids)}{duplicate} | {finding.title}"
            )

    def _show_evidence(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[evidence] No operation loaded")
            return
        artifacts = self.kernel.kernel.list_artifacts(self.operation.id)
        if not artifacts:
            journal.write("[evidence] No artifacts")
            return
        for artifact in artifacts:
            journal.write(
                f"[artifact] {artifact.id} | {artifact.kind.value} | "
                f"requirement={artifact.requirement or '-'} | "
                f"links={_artifact_links(artifact)} | sha256={artifact.sha256[:12]} | "
                f"{artifact.summary or artifact.path}"
            )

    def _show_approvals(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[approvals] No operation loaded")
            return
        approvals = self.kernel.kernel.list_approvals(self.operation.id)
        if not approvals:
            journal.write("[approvals] No approvals")
            return
        for approval in approvals:
            actor = approval.decided_by or "-"
            journal.write(
                f"[approval] {approval.id} | {approval.risk_class.value} | "
                f"{approval.status.value} | auto={str(approval.auto).lower()} | "
                f"actor={actor} | {approval.action}"
            )

    def _refresh_chrome(self) -> None:
        header = self.query_one("#operation-header", Static)
        status_bar = self.query_one("#status-bar", Static)
        if self.operation:
            operation = self.operation
            summary = self.kernel.summary(operation.id)
            header.update(
                f"SAMURAI AI  {operation.id}  {operation.mode.value.upper()}  "
                "SCOPE: OK  MODEL: NOT CONFIGURED"
            )
            status_bar.update(
                f"{operation.status.value.upper()} | TASKS {summary.tasks_done}/"
                f"{summary.tasks_total} | GATES {summary.gates_passed}/"
                f"{summary.gates_total} | PENDING {summary.pending_approvals} | "
                "APPROVAL: ASK | /help"
            )
        else:
            header.update("SAMURAI AI  NO OPERATION  SCOPE: NOT SET")
            status_bar.update("IDLE | APPROVAL: ASK | /help")


def _artifact_links(artifact: Artifact) -> str:
    links: list[str] = []
    if artifact.task_id:
        links.append(f"task={artifact.task_id}")
    if artifact.gate_id:
        links.append(f"gate={artifact.gate_id}")
    if artifact.finding_id:
        links.append(f"finding={artifact.finding_id}")
    return ",".join(links) or "-"
