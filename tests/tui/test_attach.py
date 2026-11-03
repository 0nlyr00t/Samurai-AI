"""The attach screen: a missing daemon is a notice, a live one is a stream."""

import asyncio
from pathlib import Path

from test_app import _minimal_stack

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.tui.app import SamuraiApp
from samurai_ai.tui.screens.attach import AttachScreen


def test_attach_without_a_daemon_writes_a_hint(tmp_path: Path, monkeypatch) -> None:
    stack = _minimal_stack(tmp_path)
    monkeypatch.setenv("SAMURAI_CONFIG_HOME", str(tmp_path / "config"))

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            operation = stack[0].create(
                "Attach", OperationMode.BUG_BOUNTY, ScopeContract(includes=("127.0.0.1",))
            )
            app.operation = operation
            lines: list[str] = []
            with _capture(app, lines):
                app.action_attach_session()
                await pilot.pause()
            assert any("no session daemon" in line for line in lines)
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_the_screen_formats_a_frame_compactly() -> None:
    screen = AttachScreen.__new__(AttachScreen)
    line = screen._format(
        {"seq": 4, "event": "tool.run.completed", "payload": {"tool_id": "scan.nmap"}}
    )
    assert line.startswith("[4] tool.run.completed")
    assert "scan.nmap" in line


def _capture(app: SamuraiApp, sink: list[str]):
    from unittest.mock import patch

    from textual.widgets import RichLog

    journal = app.query_one("#journal", RichLog)
    return patch.object(journal, "write", side_effect=lambda message: sink.append(str(message)))
