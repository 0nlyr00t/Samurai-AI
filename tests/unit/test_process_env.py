"""Child env is additive: a secret value must reach the process, PATH must stay."""

import asyncio

import pytest

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.execution.process import ProcessManager


def make_manager() -> ProcessManager:
    return ProcessManager(allowed_executables=frozenset({"env"}))


def test_env_reaches_the_child_process() -> None:
    manager = make_manager()
    result = asyncio.run(
        manager.run(("env",), env={"SAMURAI_TEST_VALUE": "carried-through"})
    )
    assert result.exit_code == 0
    assert "SAMURAI_TEST_VALUE=carried-through" in result.stdout.splitlines()


def test_env_is_additive_over_the_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAMURAI_PARENT_VALUE", "from-parent")
    manager = make_manager()
    result = asyncio.run(manager.run(("env",), env={"SAMURAI_CHILD_VALUE": "own"}))
    lines = result.stdout.splitlines()
    assert "SAMURAI_CHILD_VALUE=own" in lines
    assert "SAMURAI_PARENT_VALUE=from-parent" in lines
    assert any(line.startswith("PATH=") for line in lines)


def test_env_without_overrides_keeps_inherited_environment() -> None:
    manager = make_manager()
    result = asyncio.run(manager.run(("env",)))
    assert any(line.startswith("PATH=") for line in result.stdout.splitlines())


def test_nul_bytes_in_env_are_rejected() -> None:
    manager = make_manager()
    with pytest.raises(ToolValidationError, match="NUL"):
        asyncio.run(manager.run(("env",), env={"A": "b\x00c"}))
    with pytest.raises(ToolValidationError, match="NUL"):
        asyncio.run(manager.run(("env",), env={"A\x00B": "c"}))
    manager.reset_circuit_breaker()
