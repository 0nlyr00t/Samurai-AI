"""The attach screen: a missing daemon is a notice, a live one is a stream."""

import asyncio
from pathlib import Path

from test_app import _minimal_stack

from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.tui.app import SamuraiApp
from samurai_ai.tui.journal import BlockJournal
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


    journal = app.query_one("#journal", BlockJournal)
    return patch.object(journal, "write", side_effect=lambda message: sink.append(str(message)))


def test_a_resync_frame_reloads_the_snapshot_before_new_events(
    tmp_path: Path, monkeypatch
) -> None:
    """A peer told it fell behind reads state first, then continues from there."""
    monkeypatch.setattr(AttachScreen, "_run_client", lambda self: None)
    stack = _minimal_stack(tmp_path)
    stack[2].set_locale("en")
    calls: list[tuple[str, int]] = []
    appended: list[str] = []

    class _Client:
        async def subscribe(self, operation_id: str, from_seq: int = 0) -> int:
            calls.append(("subscribe", from_seq))
            return from_seq

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = AttachScreen(app.translator, Path("/tmp/x.sock"), "op_1")
            app.push_screen(screen)
            await pilot.pause()
            screen.client = _Client()
            screen._call = lambda method, params: (  # type: ignore[method-assign]
                41 if method == "operation.snapshot" else {"phase": "recon", "runs": 2}
            )
            screen._append = appended.append  # type: ignore[method-assign]
            screen._post = lambda callback, *args: callback(*args)  # type: ignore[method-assign]
            await screen._resync()
            await pilot.pause()

    asyncio.run(exercise())
    assert calls == [("subscribe", 41)]
    assert any("snapshot: phase recon" in line for line in appended)
