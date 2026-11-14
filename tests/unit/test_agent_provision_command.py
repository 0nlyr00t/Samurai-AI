"""The provision command: only for a missing binary, once per run.

The prompt always claimed the install is for a missing binary; nothing enforced
it, so a scope refusal, a bad parameter or a timeout that the model blamed on
the host still led to an install. The gate is now the typed `MissingBinaryError`
of the run's own tool call: no typed miss in this run, no install.
"""

from __future__ import annotations

from typing import Any

import pytest

from samurai_ai.agents.orchestrator import AgentOrchestrator
from samurai_ai.domain.agents import AgentRun, RequestToolProvisionCommand, RequestToolRunCommand
from samurai_ai.domain.execution import (
    ToolExecutionError,
    ToolResult,
    ToolRun,
    ToolRunStatus,
)
from samurai_ai.execution.native import MissingBinaryError

TOOL = "scan.nmap"
OTHER_TOOL = "crack.john"


class _Adapter:
    """A native adapter: it knows whether its binary is on the machine."""

    def __init__(self, available: bool = False) -> None:
        self.available = available

    def resolved_available(self) -> bool:
        return self.available


class _Registry:
    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter

    def get(self, tool_id: str) -> Any:
        if tool_id not in (TOOL, OTHER_TOOL):
            raise ToolExecutionError(f"tool not found: {tool_id}")
        return self.adapter if tool_id == TOOL else _Adapter(available=False)


class _Execution:
    def __init__(self, adapter: _Adapter, *, missing: bool = True) -> None:
        self.registry = _Registry(adapter)
        self.missing = missing
        self.tool_runs: list[str] = []

    def run_tool(self, operation_id: str, tool_id: str, params: Any, **_: Any) -> ToolResult:
        self.tool_runs.append(tool_id)
        if self.missing:
            raise MissingBinaryError(tool_id, "nmap")
        run = ToolRun(operation_id=operation_id, tool_id=tool_id, tool_version=1)
        run.status = ToolRunStatus.COMPLETED
        return ToolResult(run=run, stdout="ok", exit_code=0)


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


def _orchestrator(
    provisioner: Any, adapter: _Adapter | None = None, *, missing: bool = True
) -> tuple[AgentOrchestrator, list[tuple[str, str, str]], _Execution]:
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    execution = _Execution(adapter or _Adapter(), missing=missing)
    orchestrator.execution = execution  # type: ignore[assignment]
    orchestrator.provisioner = provisioner
    orchestrator._provisioned = set()
    orchestrator._missing_binary = set()
    notes: list[tuple[str, str, str]] = []

    def append_note(operation_id: str, run: AgentRun, command: Any) -> None:
        notes.append((operation_id, command.key, command.value))

    orchestrator._append_note = append_note  # type: ignore[method-assign]
    return orchestrator, notes, execution


def _command(tool_id: str = TOOL) -> RequestToolProvisionCommand:
    return RequestToolProvisionCommand(type="request_tool_provision", tool_id=tool_id)


def _run() -> AgentRun:
    return AgentRun(operation_id="op_1", task_id="t1", instruction="scan", specialization="recon")


def _failed_tool_run(orchestrator: AgentOrchestrator, run: AgentRun, tool_id: str = TOOL) -> None:
    """What the executor does before asking for an install: the call that misses."""
    with pytest.raises(ToolExecutionError):
        orchestrator._command_tool_run(
            "op_1",
            run,
            RequestToolRunCommand(type="request_tool_run", tool_id=tool_id, params={}),
        )


def test_provision_installs_after_a_missing_binary_and_notes_the_version() -> None:
    provisioner = _Provisioner()
    orchestrator, notes, _execution = _orchestrator(provisioner)
    run = _run()
    _failed_tool_run(orchestrator, run)

    orchestrator._command_tool_provision("op_1", run, _command())

    assert provisioner.calls == [("op_1", TOOL)]
    assert notes == [("op_1", f"provisioned.{TOOL}", "7.95")]


def test_install_without_a_tool_run_is_refused() -> None:
    """The model's hunch is not evidence: the install needs a typed miss first."""
    provisioner = _Provisioner()
    orchestrator, _notes, _execution = _orchestrator(provisioner)

    with pytest.raises(ToolExecutionError, match="missing binary"):
        orchestrator._command_tool_provision("op_1", _run(), _command())

    assert provisioner.calls == []


def test_a_scope_refusal_or_timeout_does_not_unlock_the_install() -> None:
    """`missing=False` is the scope/params/timeout case: the binary is here."""
    provisioner = _Provisioner()
    orchestrator, _notes, _execution = _orchestrator(provisioner, missing=False)
    run = _run()
    # the tool run succeeds (a rejection would look the same to the provision gate)
    orchestrator._command_tool_run(
        "op_1",
        run,
        RequestToolRunCommand(type="request_tool_run", tool_id=TOOL, params={}),
    )

    with pytest.raises(ToolExecutionError, match="already installed|missing binary"):
        orchestrator._command_tool_provision("op_1", run, _command())

    assert provisioner.calls == []


def test_a_binary_that_is_present_now_is_not_installed_again() -> None:
    """The miss happened, but the operator installed it meanwhile."""
    provisioner = _Provisioner()
    orchestrator, _notes, _execution = _orchestrator(provisioner)
    run = _run()
    _failed_tool_run(orchestrator, run)
    orchestrator.execution.registry.adapter = _Adapter(available=True)  # type: ignore[attr-defined]

    with pytest.raises(ToolExecutionError, match="already installed"):
        orchestrator._command_tool_provision("op_1", run, _command())

    assert provisioner.calls == []


def test_the_miss_of_another_tool_does_not_unlock_this_one() -> None:
    provisioner = _Provisioner()
    orchestrator, _notes, _execution = _orchestrator(provisioner)
    run = _run()
    _failed_tool_run(orchestrator, run)

    with pytest.raises(ToolExecutionError, match="missing binary"):
        orchestrator._command_tool_provision(
            "op_1",
            run,
            RequestToolProvisionCommand(type="request_tool_provision", tool_id="crack.john"),
        )

    assert provisioner.calls == []


def test_a_second_provision_in_one_run_is_refused() -> None:
    """One install per tool per run: the retry is the tool run, not another install."""
    orchestrator, _notes, _execution = _orchestrator(_Provisioner())
    command = _command()
    run = _run()
    _failed_tool_run(orchestrator, run)
    orchestrator._command_tool_provision("op_1", run, command)
    with pytest.raises(ToolExecutionError, match="already provisioned"):
        orchestrator._command_tool_provision("op_1", run, command)


def test_a_failed_install_raises_with_the_reason() -> None:
    orchestrator, _notes, _execution = _orchestrator(_Provisioner(status="failed"))
    run = _run()
    _failed_tool_run(orchestrator, run)
    with pytest.raises(ToolExecutionError, match="install failed|failed:"):
        orchestrator._command_tool_provision("op_1", run, _command())


def test_an_unknown_tool_is_refused_before_any_install() -> None:
    provisioner = _Provisioner()
    orchestrator, _notes, _execution = _orchestrator(provisioner)
    run = _run()
    _failed_tool_run(orchestrator, run)
    with pytest.raises(ToolExecutionError, match="not found"):
        orchestrator._command_tool_provision("op_1", run, _command("unknown.tool"))
    assert provisioner.calls == []


def test_without_a_provisioner_the_command_says_so() -> None:
    orchestrator, _notes, _execution = _orchestrator(None)
    run = _run()
    _failed_tool_run(orchestrator, run)
    with pytest.raises(ToolExecutionError, match="not configured"):
        orchestrator._command_tool_provision("op_1", run, _command())
