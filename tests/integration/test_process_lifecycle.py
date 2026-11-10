"""status / tail / collect: an attach never re-runs the command."""

from __future__ import annotations

import asyncio
import sys

import pytest

from samurai_ai.execution.process import ProcessManager, ProcessNotFoundError

PY = sys.executable


def _manager() -> ProcessManager:
    return ProcessManager(allowed_executables=frozenset({PY.rsplit("/", 1)[-1], "bash"}))


def test_status_tail_and_collect_read_a_finished_run() -> None:
    async def exercise() -> None:
        manager = _manager()
        handle = await manager.start([PY, "-c", "print('line-1'); print('line-2')"])
        assert manager.status(handle.id) == "running"
        result = await manager.wait(handle.id, timeout_seconds=20)
        assert result.exit_code == 0
        assert manager.status(handle.id) == "finished"
        assert "line-1" in manager.tail(handle.id)
        again = manager.collect(handle.id)  # no re-run: same result object
        assert again is result

    asyncio.run(exercise())


def test_tail_is_bounded_and_keeps_the_new_tail() -> None:
    async def exercise() -> None:
        manager = _manager()
        handle = await manager.start([PY, "-c", "print('x' * 100); print('TAIL-MARK')"])
        await manager.wait(handle.id, timeout_seconds=20)
        tail = manager.tail(handle.id, limit=12)
        assert tail.endswith("TAIL-MARK\n")
        assert len(tail) <= 12

    asyncio.run(exercise())


def test_collect_on_a_running_process_says_so() -> None:
    async def exercise() -> None:
        manager = _manager()
        handle = await manager.start([PY, "-c", "import time; time.sleep(5)"])
        with pytest.raises(ProcessNotFoundError, match="still running"):
            manager.collect(handle.id)
        await manager.cancel(handle.id)
        assert manager.collect(handle.id).cancelled is True

    asyncio.run(exercise())


def test_unknown_handle_is_unknown_not_a_rerun() -> None:
    manager = _manager()
    assert manager.status("proc_nope") == "unknown"
    with pytest.raises(ProcessNotFoundError):
        manager.collect("proc_nope")


def test_finished_results_are_capped() -> None:
    async def exercise() -> None:
        manager = _manager()
        manager.collected_limit = 2
        ids = []
        for _ in range(3):
            handle = await manager.start([PY, "-c", "print('ok')"])
            await manager.wait(handle.id, timeout_seconds=20)
            ids.append(handle.id)
        assert manager.status(ids[0]) == "unknown"
        assert manager.status(ids[-1]) == "finished"

    asyncio.run(exercise())
