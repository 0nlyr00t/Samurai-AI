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


def test_stdin_data_reaches_the_child_and_closes() -> None:
    manager = ProcessManager(allowed_executables=frozenset({"cat"}))
    result = asyncio.run(manager.run(("cat",), stdin_data=b"example.com\nwww.example.com\n"))
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["example.com", "www.example.com"]


def test_stdin_without_data_is_devnull() -> None:
    manager = ProcessManager(allowed_executables=frozenset({"cat"}))
    result = asyncio.run(manager.run(("cat",)))
    assert result.exit_code == 0
    assert result.stdout == ""


def test_oversized_stdin_is_rejected() -> None:
    manager = ProcessManager(allowed_executables=frozenset({"cat"}))
    with pytest.raises(ToolValidationError, match="stdin payload exceeds"):
        asyncio.run(manager.run(("cat",), stdin_data=b"x" * 70000))
    manager.reset_circuit_breaker()
