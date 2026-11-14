"""A failed tool result reaches the model as status, stderr and artifact ids.

Only stdout used to be fed back, and only when it was non-empty: a run that
failed with the cause on stderr and exit 1 left the executor with silence, so
the next step repeated the call. The typed exception path was already fine
(`_apply_command` rejects with its reason); the silent gap was the failed result.
"""

from __future__ import annotations

from typing import Any

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun, RequestToolRunCommand
from samurai_ai.domain.execution import ToolResult, ToolRun, ToolRunStatus


class _Registry:
    def get(self, tool_id: str) -> Any:
        return object()


class _Execution:
    registry = _Registry()

    def __init__(self, result: ToolResult) -> None:
        self.result = result

    def run_tool(self, operation_id: str, tool_id: str, params: Any, **_: Any) -> ToolResult:
        return self.result


def _orchestrator(result: ToolResult) -> tuple[AgentOrchestrator, dict[str, str]]:
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.execution = _Execution(result)  # type: ignore[assignment]
    notes: dict[str, str] = {}

    def append_note(operation_id: str, run: AgentRun, command: Any) -> None:
        notes[command.key] = command.value

    orchestrator._append_note = append_note  # type: ignore[method-assign]
    return orchestrator, notes


def _run() -> AgentRun:
    return AgentRun(operation_id="op_1", task_id="t1", instruction="scan", specialization="recon")


def _result(**kwargs: Any) -> ToolResult:
    exit_code = kwargs.pop("exit_code", 1)
    run = ToolRun(
        operation_id="op_1",
        task_id="t1",
        tool_id="scan.nmap",
        tool_version=1,
        status=ToolRunStatus.COMPLETED if exit_code == 0 else ToolRunStatus.FAILED,
        exit_code=exit_code,
    )
    return ToolResult(run=run, **kwargs)


def test_a_failed_run_feeds_status_exit_and_stderr_back() -> None:
    orchestrator, notes = _orchestrator(_result(stderr="Failed to resolve: nope.invalid"))
    orchestrator._command_tool_run(
        "op_1",
        _run(),
        RequestToolRunCommand(type="request_tool_run", tool_id="scan.nmap", params={}),
    )

    value = notes["tool-result.scan.nmap"]
    assert "status=failed" in value
    assert "exit=1" in value
    assert "Failed to resolve: nope.invalid" in value


def test_stderr_is_fed_back_even_without_stdout() -> None:
    orchestrator, notes = _orchestrator(_result(stderr="permission denied"))
    orchestrator._command_tool_run(
        "op_1",
        _run(),
        RequestToolRunCommand(type="request_tool_run", tool_id="scan.nmap", params={}),
    )

    assert "permission denied" in notes["tool-result.scan.nmap"]


def test_artifact_ids_and_stdout_survive_together() -> None:
    orchestrator, notes = _orchestrator(
        _result(stdout="80/tcp open http", artifact_ids=("art_1",))
    )
    orchestrator._command_tool_run(
        "op_1",
        _run(),
        RequestToolRunCommand(type="request_tool_run", tool_id="scan.nmap", params={}),
    )

    value = notes["tool-result.scan.nmap"]
    assert "80/tcp open http" in value
    assert "artifact_ids: art_1" in value


def test_a_completed_run_says_so_and_stays_bounded() -> None:
    orchestrator, notes = _orchestrator(
        _result(exit_code=0, stdout="a" * 5000, artifact_ids=("art_1", "art_2"))
    )
    orchestrator._command_tool_run(
        "op_1",
        _run(),
        RequestToolRunCommand(type="request_tool_run", tool_id="scan.nmap", params={}),
    )

    value = notes["tool-result.scan.nmap"]
    assert value.startswith("status=completed exit=0")
    assert len(value) < 2600
    assert "artifact_ids: art_1, art_2" in value
