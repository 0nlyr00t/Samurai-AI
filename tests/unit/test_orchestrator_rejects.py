"""An approval miss with a resolver is a hop between attempts, not a failure."""

from __future__ import annotations

from typing import Any

import pytest

from samurai_ai.agents.orchestrator import MAX_CONSECUTIVE_REJECTS, AgentOrchestrator
from samurai_ai.domain.agents import AgentRunStatus, RequestToolRunCommand


class FakeRun:
    status = AgentRunStatus.RUNNING


def orchestrator_that(reasons: list[str | None]) -> Any:
    """A bare object with just the pieces `_run_commands` touches."""
    calls: list[str] = []

    def apply(_op: str, _run: Any, command: Any, **_: Any) -> str | None:
        calls.append(command.tool_id)
        return reasons[len(calls) - 1]

    stand = object.__new__(AgentOrchestrator)
    stand._stop_requested = lambda *_a, **_k: False  # type: ignore[attr-defined]
    stand._note_repeat = None  # type: ignore[attr-defined]
    stand._apply_command = apply  # type: ignore[attr-defined]
    stand._note_site = None  # type: ignore[attr-defined]
    stand.calls = calls  # type: ignore[attr-defined]
    return stand


def commands(count: int) -> tuple[RequestToolRunCommand, ...]:
    return tuple(
        RequestToolRunCommand(type="request_tool_run", tool_id=f"t.{index}", params={})
        for index in range(count)
    )


def test_approval_misses_alone_never_trip_the_budget() -> None:
    stand = orchestrator_that(["approval-required:apr_1"] * (MAX_CONSECUTIVE_REJECTS + 4))
    rejects, consecutive = AgentOrchestrator._run_commands(
        stand,
        "op_1",
        FakeRun(),
        commands(MAX_CONSECUTIVE_REJECTS + 4),
        0,
        read_only_only=False,
        approval_resolver=lambda _id: True,
        stop_event=None,
    )
    assert consecutive == 0
    assert len(rejects) == MAX_CONSECUTIVE_REJECTS + 4


def test_without_a_resolver_the_budget_still_bites() -> None:
    stand = orchestrator_that(["approval-required:apr_1"] * (MAX_CONSECUTIVE_REJECTS + 4))
    with pytest.raises(Exception, match="too many consecutive rejected"):
        AgentOrchestrator._run_commands(
            stand,
            "op_1",
            FakeRun(),
            commands(MAX_CONSECUTIVE_REJECTS + 4),
            0,
            read_only_only=False,
            approval_resolver=None,
            stop_event=None,
        )


def test_a_real_refusal_still_counts_with_a_resolver() -> None:
    stand = orchestrator_that(["out of scope"] * (MAX_CONSECUTIVE_REJECTS + 4))
    with pytest.raises(Exception, match="too many consecutive rejected"):
        AgentOrchestrator._run_commands(
            stand,
            "op_1",
            FakeRun(),
            commands(MAX_CONSECUTIVE_REJECTS + 4),
            0,
            read_only_only=False,
            approval_resolver=lambda _id: True,
            stop_event=None,
        )
