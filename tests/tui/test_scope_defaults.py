import asyncio
from pathlib import Path

from test_app import _chat_ready_stack

from samurai_ai.tui.app import SamuraiApp


def test_new_operation_without_targets_has_empty_scope(tmp_path: Path) -> None:
    stack, _operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)):
            app._on_new_operation(("Local analysis", "bug-bounty", ""))
            assert app.operation is not None
            assert app.operation.scope.includes == ()

    asyncio.run(exercise())


def test_first_chat_without_target_does_not_authorize_loopback(tmp_path: Path) -> None:
    stack, _operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)):
            def discard_worker(coroutine, **_kwargs):  # type: ignore[no-untyped-def]
                coroutine.close()

            app.run_worker = discard_worker  # type: ignore[method-assign]
            app._start_chat("inspect local files")
            assert app.operation is not None
            assert app.operation.scope.includes == ()

    asyncio.run(exercise())


def test_shell_mode_without_operation_keeps_network_scope_empty(tmp_path: Path) -> None:
    stack, _operation = _chat_ready_stack(tmp_path, object())

    async def exercise() -> None:
        app = SamuraiApp(*stack)
        async with app.run_test(size=(120, 30)):
            await app._run_shell_mode("true")
            assert app.operation is not None
            assert app.operation.scope.includes == ()

    asyncio.run(exercise())
