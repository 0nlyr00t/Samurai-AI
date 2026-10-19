import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input, RichLog, Static

from samurai_ai.domain.kernel import ArtifactKind, PlanTask, RiskClass, StageGate
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.tui.app import SamuraiApp


def test_tui_loads_operation_and_processes_keyboard_commands(tmp_path: Path) -> None:
    database = Database(tmp_path / "tui.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
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

    async def exercise() -> None:
        app = SamuraiApp(service, kernel, operation.id)
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
                ):
                    command_input.value = command
                    await pilot.press("enter")
                    await pilot.pause()
                    assert any(expected in message for message in messages)

            assert journal.display
            await pilot.press("ctrl+o")
            assert not journal.display
            await pilot.press("ctrl+o")
            assert journal.display
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
