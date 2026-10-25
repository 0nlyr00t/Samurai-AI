from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, RichLog, Static

from samurai_ai.domain.kernel import Artifact, GateStatus, TaskStatus
from samurai_ai.domain.operations import Operation
from samurai_ai.i18n import Translator
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationNotFoundError, OperationService
from samurai_ai.services.planning import PlanningService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.redteam import RedTeamService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import InvalidLocaleError, SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
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
        background: #161b22;
    }
    #language-title {
        text-style: bold;
        color: #4fa8ff;
        padding-bottom: 1;
    }
    #language-buttons {
        height: auto;
    }
    #language-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, translator: Translator) -> None:
        super().__init__()
        self.translator = translator

    def compose(self) -> ComposeResult:
        with Vertical(id="language-dialog"):
            yield Static(self.translator.t("chooser.title"), id="language-title")
            with Horizontal(id="language-buttons"):
                yield Button(self.translator.t("chooser.english"), id="lang-en")
                yield Button(self.translator.t("chooser.russian"), id="lang-ru")

    def on_mount(self) -> None:
        self.query_one("#lang-en", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "lang-ru":
            self.dismiss("ru")
        else:
            self.dismiss("en")


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
        self.requested_operation_id = operation_id
        self.operation: Operation | None = None
        self.translator = Translator(settings.get_locale() or "en")
        self._locale_known = settings.get_locale() is not None

    def compose(self) -> ComposeResult:
        yield Static(id="operation-header")
        yield RichLog(id="journal", markup=False, wrap=True, auto_scroll=True)
        yield Input(placeholder=self.translator.t("input.placeholder"), id="command-input")
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
        journal = self.query_one("#journal", RichLog)
        journal.write(self.translator.t("welcome.title"))
        journal.write(self.translator.t("welcome.stage"))
        if self.operation:
            journal.write(
                self.translator.t(
                    "welcome.loaded", operation_id=self.operation.id, name=self.operation.name
                )
            )
        else:
            journal.write(self.translator.t("welcome.no-operation"))
        journal.write(self.translator.t("welcome.help-hint"))

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
            for line in self.translator.help_lines():
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

        if name.startswith("/"):
            journal.write(
                "[error] " + self.translator.t("error.unknown-command", name=name)
            )
        else:
            journal.write("[info] " + self.translator.t("info.ai-disabled"))

    def _show_status(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[status] " + self.translator.t("status.no-operation"))
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
            journal.write("[scope] " + self.translator.t("status.no-operation"))
            return
        scope = self.operation.scope
        if not target:
            journal.write(
                "[scope] "
                + self.translator.t("scope.includes", rules=", ".join(scope.includes))
            )
            excludes = ", ".join(scope.excludes) or self.translator.t("scope.none")
            journal.write(
                "[scope] " + self.translator.t("scope.excludes", rules=excludes)
            )
            return
        decision = scope.evaluate(target)
        level = "allow" if decision.allowed else "deny"
        journal.write(
            f"[{level}] {decision.target}: {decision.reason.value} — {decision.detail}"
        )

    def _show_plan(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[plan] " + self.translator.t("status.no-operation"))
            return
        operation = self.operation
        objective = operation.objective or self.translator.t("plan.objective-unset")
        plan = self.kernel.get_current_plan(operation.id)
        journal.write(
            "[plan] "
            + self.translator.t(
                "plan.stage", stage=operation.status.value, objective=objective
            )
        )
        if plan is None:
            journal.write("[plan] " + self.translator.t("plan.no-plan"))
            return
        approved = self.translator.t(
            "plan.approved" if plan.approved else "plan.not-approved"
        )
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
            journal.write("[tasks] " + self.translator.t("status.no-operation"))
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.tasks:
            journal.write("[tasks] " + self.translator.t("tasks.empty"))
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
            journal.write("[gates] " + self.translator.t("status.no-operation"))
            return
        plan = self.kernel.get_current_plan(self.operation.id)
        if plan is None or not plan.gates:
            journal.write("[gates] " + self.translator.t("gates.empty"))
            return
        for gate in plan.gates:
            requirements = ",".join(gate.required_artifacts) or "-"
            journal.write(
                f"[gate] {gate.id} | {gate.status.value} | artifacts={requirements} | {gate.name}"
            )

    def _show_findings(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[findings] " + self.translator.t("status.no-operation"))
            return
        findings = self.kernel.kernel.list_findings(self.operation.id)
        if not findings:
            journal.write("[findings] " + self.translator.t("findings.empty"))
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
            journal.write("[evidence] " + self.translator.t("status.no-operation"))
            return
        artifacts = self.kernel.kernel.list_artifacts(self.operation.id)
        if not artifacts:
            journal.write("[evidence] " + self.translator.t("evidence.empty"))
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
            journal.write("[approvals] " + self.translator.t("status.no-operation"))
            return
        approvals = self.kernel.kernel.list_approvals(self.operation.id)
        if not approvals:
            journal.write("[approvals] " + self.translator.t("approvals.empty"))
            return
        for approval in approvals:
            actor = approval.decided_by or "-"
            journal.write(
                f"[approval] {approval.id} | {approval.risk_class.value} | "
                f"{approval.status.value} | auto={str(approval.auto).lower()} | "
                f"actor={actor} | {approval.action}"
            )

    def _show_snapshots(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[snapshots] " + self.translator.t("status.no-operation"))
            return
        snapshots = self.recovery.list_snapshots(self.operation.id)
        if not snapshots:
            journal.write("[snapshots] " + self.translator.t("snapshots.empty"))
            return
        for snapshot in snapshots:
            journal.write(
                f"[snapshot] {snapshot.id} | {snapshot.label or '-'} | "
                f"{snapshot.created_at.isoformat()}"
            )

    def _show_tools(self) -> None:
        journal = self.query_one("#journal", RichLog)
        definitions = self.execution.list_tools()
        if not definitions:
            journal.write("[tools] " + self.translator.t("tools.empty"))
            return
        for definition in definitions:
            network = (
                self.translator.t("tools.network")
                if definition.network_access
                else self.translator.t("tools.no-network")
            )
            journal.write(
                f"[tool] {definition.id} | v{definition.version} | {definition.kind.value} | "
                f"risk={definition.risk_class.value} | {network} | {definition.description}"
            )

    def _show_runs(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[runs] " + self.translator.t("status.no-operation"))
            return
        runs = self.execution.list_runs(self.operation.id)
        if not runs:
            journal.write("[runs] " + self.translator.t("runs.empty"))
            return
        for run in runs:
            artifacts = ",".join(
                artifact_id
                for artifact_id in (run.stdout_artifact_id, run.stderr_artifact_id)
                if artifact_id
            ) or "-"
            warnings = ",".join(run.injection_warnings) or "-"
            journal.write(
                f"[run] {run.id} | {run.tool_id} v{run.tool_version} | {run.status.value} | "
                f"trust={run.trust.value} | warnings={warnings} | artifacts={artifacts}"
            )

    def _show_agents(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[agents] " + self.translator.t("status.no-operation"))
            return
        runs = self.agents.list_runs(self.operation.id)
        if not runs:
            journal.write("[agents] " + self.translator.t("agents.empty"))
            return
        for run in runs:
            journal.write(
                f"[agent] {run.id} | {run.task_id} | {run.status.value} | "
                f"depth={run.depth} | route={','.join(run.model_route)}"
            )

    def _show_swarms(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[swarm] " + self.translator.t("status.no-operation"))
            return
        swarms = self.swarms.list_swarms(self.operation.id)
        if not swarms:
            journal.write("[swarm] " + self.translator.t("swarm.empty"))
            return
        for swarm in swarms:
            journal.write(
                f"[swarm] {swarm.id} | {swarm.status.value} | workers={swarm.workers} | "
                f"tasks={','.join(swarm.task_ids)}"
            )
            for member in self.agents.list_swarm_members(swarm.id):
                journal.write(
                    f"  [member] {member.task_id} | {member.id} | {member.status.value} | "
                    f"{member.specialization.value}"
                )

    def _show_secrets(self) -> None:
        journal = self.query_one("#journal", RichLog)
        operation_id = self.operation.id if self.operation else None
        entries = self.vault.list_metadata(operation_id)
        if not entries:
            journal.write("[secrets] " + self.translator.t("secrets.empty"))
            return
        for entry in entries:
            scope = entry.operation_id or self.translator.t("secrets.global")
            journal.write(
                f"[secret] {entry.id} | {entry.name} | scope={scope} | "
                f"by={entry.created_by} | {entry.created_at.isoformat()}"
            )

    def _show_assets(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[assets] " + self.translator.t("status.no-operation"))
            return
        entries = self.playbooks.list_assets(self.operation.id)
        if not entries:
            journal.write("[assets] " + self.translator.t("assets.empty"))
            return
        for entry in entries:
            journal.write(
                f"[asset] {entry.id} | {entry.kind.value} | {entry.value} | {entry.source}"
            )

    def _show_reports(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[report] " + self.translator.t("status.no-operation"))
            return
        reports = self.reports.reports.list_reports(self.operation.id)
        if not reports:
            journal.write("[report] " + self.translator.t("report.empty"))
            return
        for report in reports:
            journal.write(
                f"[report] {report.id} | {report.format.value} | "
                f"findings={len(report.finding_ids)} | {report.created_at.isoformat()}"
            )

    def _show_hypotheses(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[hypotheses] " + self.translator.t("status.no-operation"))
            return
        hypotheses = self.ctf.list_hypotheses(self.operation.id)
        if not hypotheses:
            journal.write("[hypotheses] " + self.translator.t("hypotheses.empty"))
            return
        for item in hypotheses:
            journal.write(
                f"[hypothesis] {item.id} | {item.status.value} | {item.title}"
            )

    def _show_flags(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[flags] " + self.translator.t("status.no-operation"))
            return
        flags = self.ctf.list_flags(self.operation.id)
        if not flags:
            journal.write("[flags] " + self.translator.t("flags.empty"))
            return
        for flag in flags:
            journal.write(f"[flag] {flag.id} | {flag.name} | {flag.value}")

    def _show_attack_path(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[attack-path] " + self.translator.t("status.no-operation"))
            return
        steps = self.redteam.list_steps(self.operation.id)
        if not steps:
            journal.write("[attack-path] " + self.translator.t("attack-path.empty"))
            return
        for step in steps:
            journal.write(
                f"[step] {step.id} | {step.status.value} | {step.technique_id} | "
                f"{step.name} | detection={step.detection.value}"
            )

    def _show_debriefs(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[debrief] " + self.translator.t("status.no-operation"))
            return
        debriefs = self.redteam.list_debriefs(self.operation.id)
        if not debriefs:
            journal.write("[debrief] " + self.translator.t("debrief.empty"))
            return
        for debrief in debriefs:
            journal.write(
                f"[debrief] {debrief.id} | {debrief.format.value} | "
                f"steps={debrief.step_count} | gaps={len(debrief.open_gaps)} | "
                f"{debrief.created_at.isoformat()}"
            )

    def _show_test_cases(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[test-cases] " + self.translator.t("status.no-operation"))
            return
        cases = self.planning.list_test_cases(self.operation.id)
        if not cases:
            journal.write("[test-cases] " + self.translator.t("test-cases.empty"))
            return
        for case in cases:
            marker = " | STOP" if case.stop_before else ""
            journal.write(
                f"[test-case] {case.id} | {case.status.value} | "
                f"{case.technique_id} | {case.kill_chain_phase.value} | "
                f"{case.name}{marker}"
            )

    def _show_runbooks(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[runbook] " + self.translator.t("status.no-operation"))
            return
        runbooks = self.planning.list_runbooks(self.operation.id)
        if not runbooks:
            journal.write("[runbook] " + self.translator.t("runbook.empty"))
            return
        for runbook in runbooks:
            journal.write(
                f"[runbook] {runbook.id} | {runbook.format.value} | "
                f"cases={len(runbook.test_case_ids)} | stops={runbook.stop_points} | "
                f"{runbook.created_at.isoformat()}"
            )

    def _show_reconcile(self) -> None:
        journal = self.query_one("#journal", RichLog)
        if not self.operation:
            journal.write("[reconcile] " + self.translator.t("status.no-operation"))
            return
        marked_runs = self.execution.reconcile_runs(self.operation.id, actor="operator")
        report = self.recovery.reconcile_operation(self.operation.id, actor="operator")
        none = self.translator.t("list.none")
        journal.write(
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
        journal = self.query_one("#journal", RichLog)
        if not argument:
            journal.write(
                "[lang] "
                + self.translator.t("lang.current", locale=self.translator.locale)
            )
            return
        try:
            locale = self.settings.set_locale(argument)
        except InvalidLocaleError:
            journal.write(
                "[error] " + self.translator.t("lang.invalid", locale=argument)
            )
            return
        self.translator.set_locale(locale)
        self.query_one("#command-input", Input).placeholder = self.translator.t(
            "input.placeholder"
        )
        journal.write("[lang] " + self.translator.t("lang.changed", locale=locale))
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
            status_bar.update(
                self.translator.t(
                    "statusbar.with-operation",
                    status=operation.status.value.upper(),
                    tasks_done=summary.tasks_done,
                    tasks_total=summary.tasks_total,
                    gates_passed=summary.gates_passed,
                    gates_total=summary.gates_total,
                    pending=summary.pending_approvals,
                )
            )
        else:
            header.update(self.translator.t("header.no-operation"))
            status_bar.update(self.translator.t("statusbar.idle"))


def _artifact_links(artifact: Artifact) -> str:
    links: list[str] = []
    if artifact.task_id:
        links.append(f"task={artifact.task_id}")
    if artifact.gate_id:
        links.append(f"gate={artifact.gate_id}")
    if artifact.finding_id:
        links.append(f"finding={artifact.finding_id}")
    return ",".join(links) or "-"
