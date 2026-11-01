"""A tool description says what the model would otherwise learn from a refusal."""

from __future__ import annotations

from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import ProcessExecTool, ShellExecTool


def test_process_exec_lists_its_allowlist() -> None:
    tool = ProcessExecTool(ProcessManager(allowed_executables=frozenset({"echo", "cat"})))

    assert "allowlist: cat, echo" in tool.definition.description


def test_shell_exec_says_it_is_a_shell() -> None:
    assert "bash -lc" in ShellExecTool().definition.description
