import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input, RichLog, Static

from samurai_ai.domain.agents import AgentRun, AgentRunStatus
from samurai_ai.domain.ctf import FlagRecord, Hypothesis
from samurai_ai.domain.kernel import ArtifactKind, PlanTask, RiskClass, StageGate
from samurai_ai.domain.operations import OperationEvent, OperationMode
from samurai_ai.domain.playbooks import AssetKind, AssetLedgerEntry
from samurai_ai.domain.reports import BountyReport, ReportFormat
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.ctf import CtfService
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.playbooks import PlaybookService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.reports import ReportService
from samurai_ai.services.settings import SettingsService
from samurai_ai.services.vault import VaultService
from samurai_ai.storage.agents import AgentRunRepository
from samurai_ai.storage.ctf import CtfRepository
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.playbooks import PlaybookRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.reports import ReportRepository
from samurai_ai.storage.vault import VaultRepository
from samurai_ai.tui.app import SamuraiApp


def test_tui_loads_operation_and_processes_keyboard_commands(tmp_path: Path) -> None:
    database = Database(tmp_path / "tui.db")
    operation_repository = OperationRepository(database)
    service = OperationService(operation_repository)
    kernel = KernelService(service, KernelRepository(database))
    recovery_repository = RecoveryRepository(database)
    settings = SettingsService(recovery_repository)
    settings.set_locale("en")
    recovery = RecoveryService(service, kernel, recovery_repository)
    vault = VaultService(
        VaultRepository(database), operation_repository, tmp_path / "vault.key"
    )
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), vault=vault
    )
    agents = AgentRunRepository(database)
    playbooks = PlaybookService(service, kernel, PlaybookRepository(database))
    reports = ReportService(service, kernel, playbooks, ReportRepository(database))
    ctf = CtfService(service, kernel, CtfRepository(database))
    operation = service.create(
        "TUI lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("*.example.com",)),
        objective="Test the operator workflow",
    )
    kernel.create_plan(
        operation.id,
        "TUI kernel plan",
        [
            PlanTask(
                id="recon",
                title="Map the service",
                risk_class=RiskClass.R1,
                required_artifacts=("service-map",),
            )
        ],
        [StageGate(id="recon-complete", name="Recon complete")],
    )
    artifact = kernel.register_artifact(
        operation.id,
        ArtifactKind.NOTE,
        "service exposed on 443",
        summary="Service map",
        requirement="service-map",
        task_id="recon",
    )
    finding = kernel.create_finding(operation.id, "TUI finding")
    approval = kernel.request_approval(
        operation.id,
        "Run active lab probe",
        RiskClass.R2,
    )
    tool_result = execution.run_tool(operation.id, "local.echo", {"text": "hello tui"})
    secret = vault.add(
        "tui-token", "tui-secret-plaintext", operation.id, created_by="operator"
    )
    agent_run = AgentRun(
        operation_id=operation.id,
        task_id="recon",
        model_route=("fake/agent@v1",),
    )
    agents.add(agent_run)
    agent_run.finish(AgentRunStatus.COMPLETED)
    agents.finish(agent_run)
    playbooks.playbooks.add_asset(
        AssetLedgerEntry(
            operation_id=operation.id, kind=AssetKind.DOMAIN, value="www.example.com"
        ),
        OperationEvent(operation_id=operation.id, event_type="asset.recorded", payload={}),
    )
    reports.reports.add_report(
        BountyReport(
            operation_id=operation.id,
            format=ReportFormat.MARKDOWN,
            sha256="ab" * 32,
            size_bytes=2,
            path="reports/tui/rep_tui.md",
            finding_ids=("find_1",),
            asset_count=1,
        ),
        OperationEvent(operation_id=operation.id, event_type="report.generated", payload={}),
    )
    hypothesis = Hypothesis(operation_id=operation.id, title="TUI hypothesis")
    ctf.ctf.add_hypothesis(
        hypothesis,
        OperationEvent(
            operation_id=operation.id, event_type="hypothesis.proposed", payload={}
        ),
    )
    ctf.ctf.add_flag(
        FlagRecord(operation_id=operation.id, name="user", value="HTB{tui_fl4g}"),
        OperationEvent(operation_id=operation.id, event_type="flag.captured", payload={}),
    )

    async def exercise() -> None:
        app = SamuraiApp(
            service, kernel, settings, recovery, execution, vault, agents,
            playbooks, reports, ctf, operation.id,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            header = app.query_one("#operation-header", Static)
            assert operation.id in str(header.render())

            journal = app.query_one("#journal", RichLog)
            command_input = app.query_one("#command-input", Input)
            messages: list[str] = []
            with patch.object(
                journal, "write", side_effect=lambda message: messages.append(message)
            ):
                command_input.value = "/scope sub.example.com"
                await pilot.press("enter")
                await pilot.pause()
                assert any("[allow]" in message for message in messages)

                command_input.value = "/status"
                await pilot.press("enter")
                await pilot.pause()
                assert any("version=1" in message for message in messages)

                command_input.value = "/plan"
                await pilot.press("enter")
                await pilot.pause()
                assert any("Test the operator workflow" in message for message in messages)

                for command, expected in (
                    ("/tasks", "recon"),
                    ("/gates", "recon-complete"),
                    ("/findings", finding.id),
                    ("/evidence", artifact.id),
                    ("/approvals", approval.id),
                    ("/tools", "local.echo"),
                    ("/tools", "sandbox.exec"),
                    ("/runs", tool_result.run.id),
                    ("/agents", agent_run.id),
                    ("/secrets", secret.id),
                    ("/assets", "www.example.com"),
                    ("/report", "findings=1"),
                    ("/hypotheses", hypothesis.id),
                    ("/flags", "HTB{tui_fl4g}"),
                ):
                    command_input.value = command
                    await pilot.press("enter")
                    await pilot.pause()
                    assert any(expected in message for message in messages)

                assert not any("tui-secret-plaintext" in message for message in messages)

            assert journal.display
            await pilot.press("ctrl+o")
            assert not journal.display
            await pilot.press("ctrl+o")
            assert journal.display
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
