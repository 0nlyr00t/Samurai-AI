import asyncio
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input, RichLog, Static

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.operations import OperationService
from samurai_ai.storage.database import Database
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.tui.app import SamuraiApp


def test_tui_loads_operation_and_processes_keyboard_commands(tmp_path: Path) -> None:
    service = OperationService(OperationRepository(Database(tmp_path / "tui.db")))
    operation = service.create(
        "TUI lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("*.example.com",)),
        objective="Test the operator workflow",
    )

    async def exercise() -> None:
        app = SamuraiApp(service, operation.id)
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

            assert journal.display
            await pilot.press("ctrl+o")
            assert not journal.display
            await pilot.press("ctrl+o")
            assert journal.display
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
