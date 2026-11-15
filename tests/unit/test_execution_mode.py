from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from samurai_ai.agents.orchestrator import (
    AgentOrchestrator,
    AgentOutputError,
    _ensure_complete_tool_calls,
)
from samurai_ai.domain.agents import AgentRunStatus, RequestToolRunCommand
from samurai_ai.domain.execution import ToolDefinition, ToolKind
from samurai_ai.domain.kernel import RiskClass


class _Adapter:
    def __init__(
        self,
        tool_id: str,
        mode: str = "parallel",
        *,
        capabilities: tuple[str, ...] = (),
    ) -> None:
        self.definition = ToolDefinition(
            id=tool_id,
            version=1,
            kind=ToolKind.LOCAL,
            read_only=True,
            execution_mode=mode,
            risk_class=RiskClass.R1,
            capabilities=capabilities,
        )


class _Registry:
    def __init__(self, adapters: list[_Adapter]) -> None:
        self.adapters = {adapter.definition.id: adapter for adapter in adapters}

    def get(self, tool_id: str) -> _Adapter:
        return self.adapters[tool_id]


def _orchestrator(mode: str = "parallel") -> tuple[AgentOrchestrator, SimpleNamespace, list[int]]:
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.execution = SimpleNamespace(
        registry=_Registry([_Adapter("recon.one", mode), _Adapter("recon.two", mode)])
    )
    active = 0
    peak = [0]
    lock = threading.Lock()

    def apply(_operation_id, _run, _command, **_kwargs):
        nonlocal active
        with lock:
            active += 1
            peak[0] = max(peak[0], active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return None

    orchestrator._apply_command = apply
    orchestrator._stop_requested = lambda _run, _event: False
    run = SimpleNamespace(id="run-1", operation_id="op-1", status=AgentRunStatus.RUNNING)
    return orchestrator, run, peak


def test_independent_read_only_calls_run_in_parallel() -> None:
    orchestrator, run, peak = _orchestrator()
    commands = (
        RequestToolRunCommand(type="request_tool_run", tool_id="recon.one", params={}),
        RequestToolRunCommand(type="request_tool_run", tool_id="recon.two", params={}),
    )
    rejects, _ = orchestrator._run_commands(
        "op-1",
        run,
        commands,
        0,
        read_only_only=True,
        approval_resolver=None,
        stop_event=None,
    )
    assert rejects == []
    assert peak[0] == 2


def test_sequential_tool_forces_the_batch_back_to_serial_execution() -> None:
    orchestrator, run, peak = _orchestrator(mode="sequential")
    commands = (
        RequestToolRunCommand(type="request_tool_run", tool_id="recon.one", params={}),
        RequestToolRunCommand(type="request_tool_run", tool_id="recon.two", params={}),
    )
    rejects, _ = orchestrator._run_commands(
        "op-1",
        run,
        commands,
        0,
        read_only_only=True,
        approval_resolver=None,
        stop_event=None,
    )
    assert rejects == []
    assert peak[0] == 1


def test_truncated_tool_calls_are_not_executed() -> None:
    command = RequestToolRunCommand(type="request_tool_run", tool_id="recon.one", params={})
    with pytest.raises(AgentOutputError, match="truncated"):
        _ensure_complete_tool_calls((command,), "length")
    _ensure_complete_tool_calls((command,), "stop")


def test_session_control_is_sequential_by_contract() -> None:
    from samurai_ai.execution.session_tools import SessionControlTool

    assert SessionControlTool().definition.execution_mode == "sequential"


def test_subagent_cannot_execute_privileged_session_tools() -> None:
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator.execution = SimpleNamespace(
        registry=_Registry(
            [_Adapter("session.control", "sequential", capabilities=("admin", "sessions"))]
        )
    )
    run = SimpleNamespace(
        id="child-1",
        operation_id="op-1",
        parent_run_id="parent-1",
        status=AgentRunStatus.RUNNING,
    )
    called = []
    orchestrator._command_tool_run = lambda *_args, **_kwargs: called.append(True)
    rejected = []
    orchestrator._reject = lambda _operation_id, _run, _command, reason: rejected.append(reason)
    command = RequestToolRunCommand(
        type="request_tool_run", tool_id="session.control", params={"action": "open"}
    )
    reason = orchestrator._apply_command("op-1", run, command)
    assert reason == "privileged execution belongs to the parent agent"
    assert called == []
    assert rejected == [reason]


def test_interrupted_run_is_settled_without_replaying_tools() -> None:
    from samurai_ai.domain.agents import AgentRun
    from samurai_ai.domain.kernel import TaskStatus

    orchestrator = object.__new__(AgentOrchestrator)
    run = AgentRun(operation_id="op-1", task_id="task-1", model_route=("model",))
    orchestrator.get_run = lambda _run_id: run
    orchestrator._task_status = lambda _operation_id, _task_id: TaskStatus.IN_PROGRESS
    finished = []
    orchestrator._finish_failed = lambda current, status, reason: finished.append((status, reason))
    orchestrator._execute_steps = lambda *_args, **_kwargs: pytest.fail("must not resume")
    orchestrator._append_event = lambda _event: None
    result = orchestrator.repair_interrupted(run.id)
    assert result is run
    assert finished == [
        (
            AgentRunStatus.FAILED,
            "interrupted before settlement; start a new run instead of replaying",
        )
    ]


def test_provider_length_reason_is_preserved() -> None:
    from samurai_ai.agents.openai_compat import parse_completion

    response = parse_completion(
        "openai/test@v1",
        "openai",
        {
            "choices": [
                {
                    "message": {"content": "{\"commands\":[]}"},
                    "finish_reason": "length",
                }
            ]
        },
        1,
    )
    assert response.stop_reason == "length"
