"""The provision command: only for a missing binary, once per run."""

from __future__ import annotations

from typing import Any

import pytest

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun, RequestToolProvisionCommand
from samurai_ai.domain.execution import ToolExecutionError


class _Registry:
    def get(self, tool_id: str) -> Any:
        if tool_id != "scan.nmap":
            raise ToolExecutionError(f"tool not found: {tool_id}")
        return object()


class _Execution:
    registry = _Registry()


class _State:
    installed_version = "7.95"


class _Provisioner:
    def __init__(self, status: str = "ready") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

        class _Detector:
            def detect(self, refresh: bool = False) -> Any:
                return object()

        self.detector = _Detector()

    def run(self, operation_id: str, tool_id: str, profile: Any) -> Any:
        self.calls.append((operation_id, tool_id))
        run = type("Run", (), {})()
        run.status = type("Status", (), {"value": self.status})()
        run.error = "" if self.status == "ready" else "brew install nmap exited 1: boom"
        return run

    def state(self, tool_id: str) -> _State:
        return _State()


class _Operations:
    def __init__(self) -> None:
        self.events: list[object] = []

    def repository(self) -> None:  # pragma: no cover - attribute access only
        return None


def _orchestrator(provisioner: Any) -> tuple[AgentOrchestrator, list[tuple[str, str, str]]]:
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator.execution = _Execution()
    orchestrator.provisioner = provisioner
    orchestrator._provisioned = set()
    notes: list[tuple[str, str, str]] = []

    def append_note(operation_id: str, run: AgentRun, command: Any) -> None:
        notes.append((operation_id, command.key, command.value))

    orchestrator._append_note = append_note  # type: ignore[method-assign]
    return orchestrator, notes


def _command(tool_id: str = "scan.nmap") -> RequestToolProvisionCommand:
    return RequestToolProvisionCommand(type="request_tool_provision", tool_id=tool_id)


def _run() -> AgentRun:
    return AgentRun(operation_id="op_1", task_id="t1", instruction="scan", specialization="recon")


def test_a_provision_install_and_notes_the_version() -> None:
    provisioner = _Provisioner()
    orchestrator, notes = _orchestrator(provisioner)
    orchestrator._command_tool_provision("op_1", _run(), _command())
    assert provisioner.calls == [("op_1", "scan.nmap")]
    assert notes == [("op_1", "provisioned.scan.nmap", "7.95")]


def test_a_second_provision_in_one_run_is_refused() -> None:
    """One install per tool per run: the retry is the tool run, not another install."""
    orchestrator, _notes = _orchestrator(_Provisioner())
    command = _command()
    run = _run()
    orchestrator._command_tool_provision("op_1", run, command)
    with pytest.raises(ToolExecutionError, match="already provisioned"):
        orchestrator._command_tool_provision("op_1", run, command)


def test_a_failed_install_raises_with_the_reason() -> None:
    orchestrator, _notes = _orchestrator(_Provisioner(status="failed"))
    with pytest.raises(ToolExecutionError, match="install failed|failed:"):
        orchestrator._command_tool_provision(
            "op_1",
            _run(),
            RequestToolProvisionCommand(type="request_tool_provision", tool_id="scan.nmap"),
        )


def test_an_unknown_tool_is_refused_before_any_install() -> None:
    provisioner = _Provisioner()
    orchestrator, _notes = _orchestrator(provisioner)
    with pytest.raises(ToolExecutionError, match="not found"):
        orchestrator._command_tool_provision("op_1", _run(), _command("crack.john"))
    assert provisioner.calls == []


def test_without_a_provisioner_the_command_says_so() -> None:
    orchestrator, _notes = _orchestrator(None)
    with pytest.raises(ToolExecutionError, match="not configured"):
        orchestrator._command_tool_provision(
            "op_1",
            _run(),
            RequestToolProvisionCommand(type="request_tool_provision", tool_id="scan.nmap"),
        )
