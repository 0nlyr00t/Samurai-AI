import asyncio
import os
import sys
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.execution.process import (
    CircuitBreakerOpenError,
    ProcessManager,
)

PYTHON = os.path.basename(sys.executable)


def python_manager(**kwargs: object) -> ProcessManager:
    return ProcessManager(allowed_executables=frozenset({"echo", PYTHON}), **kwargs)  # type: ignore[arg-type]


def test_process_success_captures_output() -> None:
    async def exercise() -> None:
        manager = python_manager()
        result = await manager.run(["echo", "hello"])
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert not result.timed_out
        assert not result.truncated
        assert manager.list() == []

    asyncio.run(exercise())


def test_process_timeout_kills_process() -> None:
    async def exercise() -> None:
        manager = python_manager()
        handle = await manager.start(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        assert len(manager.list()) == 1
        result = await manager.wait(handle.id, timeout_seconds=0.2)
        assert result.timed_out
        assert result.exit_code is not None
        assert manager.list() == []

    asyncio.run(exercise())


def test_process_cancel_terminates_process() -> None:
    async def exercise() -> None:
        manager = python_manager()
        handle = await manager.start(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        result = await manager.cancel(handle.id)
        assert result.cancelled
        assert manager.list() == []

    asyncio.run(exercise())


def test_process_output_is_capped() -> None:
    async def exercise() -> None:
        manager = python_manager(output_limit_bytes=16)
        result = await manager.run(
            [sys.executable, "-c", "print('x' * 10000)"],
        )
        assert result.exit_code == 0
        assert result.truncated
        assert len(result.stdout.encode()) <= 16

    asyncio.run(exercise())


def test_process_never_uses_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"

    async def exercise() -> None:
        manager = python_manager()
        result = await manager.run(
            ["echo", f"hello; touch {marker} && $(touch {marker})"]
        )
        assert result.exit_code == 0
        assert "touch" in result.stdout

    asyncio.run(exercise())
    assert not marker.exists()


def test_executable_allowlist_is_enforced() -> None:
    async def exercise() -> None:
        manager = ProcessManager()
        with pytest.raises(ToolValidationError, match="allowlist"):
            await manager.run([sys.executable, "-c", "pass"])
        with pytest.raises(ToolValidationError):
            await manager.run([])

    asyncio.run(exercise())


def test_circuit_breaker_opens_after_consecutive_failures() -> None:
    async def exercise() -> None:
        manager = python_manager(circuit_breaker_threshold=2)
        for _ in range(2):
            result = await manager.run(
                [sys.executable, "-c", "import sys; sys.exit(1)"]
            )
            assert result.exit_code == 1
        with pytest.raises(CircuitBreakerOpenError):
            await manager.run(["echo", "blocked"])
        manager.reset_circuit_breaker()
        result = await manager.run(["echo", "again"])
        assert result.exit_code == 0

    asyncio.run(exercise())


def test_circuit_breaker_resets_on_success() -> None:
    async def exercise() -> None:
        manager = python_manager(circuit_breaker_threshold=2)
        await manager.run([sys.executable, "-c", "import sys; sys.exit(1)"])
        await manager.run(["echo", "fine"])
        await manager.run([sys.executable, "-c", "import sys; sys.exit(1)"])
        result = await manager.run(["echo", "still-open"])
        assert result.exit_code == 0

    asyncio.run(exercise())
