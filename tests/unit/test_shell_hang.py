"""shell.exec hang handling: a killed command gets an actionable status.

stdin is closed and there is no controlling terminal (see the ProcessManager
regression test), so a kill is a genuine block — a command that waited on a
prompt or a connection. The note has to tell the agent that, and name the fix,
or the agent re-runs the same hanging command and burns another window.
"""

from __future__ import annotations

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import ShellExecTool, stall_as_timeout


class _ResultManager:
    """Stands in for ProcessManager, returning a fixed ProcessResult."""

    def __init__(self, result: ProcessResult) -> None:
        self._result = result
        self.stall_seconds = 0.0

    async def run(self, argv, timeout_seconds=None):  # type: ignore[no-untyped-def]
        return self._result


def _invoke(result: ProcessResult):  # type: ignore[no-untyped-def]
    tool = ShellExecTool(manager=_ResultManager(result))  # type: ignore[arg-type]
    return tool.invoke({"command": "cat"}, ScopeContract(includes=("10.0.0.5",)))


def test_stalled_run_is_a_timeout_with_interactive_guidance() -> None:
    timed_out, note = stall_as_timeout(
        ProcessResult(exit_code=-15, stdout="", stderr="", stalled=True)
    )
    assert timed_out
    assert "no output" in note
    assert "non-interactive" in note
    assert "do not re-run it unchanged" in note


def test_timeout_without_output_reads_as_a_blocked_command() -> None:
    timed_out, note = stall_as_timeout(
        ProcessResult(exit_code=-15, stdout="", stderr="", timed_out=True)
    )
    assert timed_out
    assert "no output" in note
    assert "blocked connection" in note


def test_timeout_with_output_reads_as_a_long_command() -> None:
    timed_out, note = stall_as_timeout(
        ProcessResult(exit_code=-15, stdout="scanning...\n", stderr="", timed_out=True)
    )
    assert timed_out
    assert "long scan" in note
    # output was produced, so the note must not blame interactivity
    assert "waiting on input" not in note


def test_clean_exit_has_no_note() -> None:
    timed_out, note = stall_as_timeout(
        ProcessResult(exit_code=0, stdout="ok", stderr="")
    )
    assert not timed_out
    assert note == ""


def test_shell_exec_surfaces_the_stall_note_and_flags_timeout() -> None:
    output = _invoke(ProcessResult(exit_code=-15, stdout="", stderr="", stalled=True))
    assert output.timed_out
    assert output.exit_code == -15
    assert "non-interactive" in output.stderr


def test_shell_exec_description_warns_it_is_non_interactive() -> None:
    description = ShellExecTool().definition.description
    assert "non-interactively" in description
    assert "stdin is closed" in description
