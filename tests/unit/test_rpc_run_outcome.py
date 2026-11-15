"""A daemon-owned run reports what its work actually achieved."""

from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.domain.swarm import SwarmStatus
from samurai_ai.services.rpc.runs import STATE_DONE, STATE_PARTIAL, RunHandle


def _supervised(outcome: Any) -> RunHandle:
    from samurai_ai.services.rpc.runs import RunRegistry

    async def work(_handle: RunHandle) -> Any:
        return outcome

    async def run() -> RunHandle:
        registry = RunRegistry(work, lambda *_args: None)
        handle = registry.start("op_1", ["fake/agent@v1"], None, "operator")
        await asyncio.gather(*list(registry._tasks.values()))
        return handle

    return asyncio.run(run())


def test_a_completed_swarm_is_a_done_run() -> None:
    assert _supervised(SwarmStatus.COMPLETED).state == STATE_DONE


def test_a_partial_swarm_is_not_a_done_run() -> None:
    """Blocked or failed members used to be reported as a finished daemon run."""
    handle = _supervised(SwarmStatus.PARTIAL)
    assert handle.state == STATE_PARTIAL
    assert handle.detail


def test_a_failed_swarm_is_a_failed_run() -> None:
    assert _supervised(SwarmStatus.FAILED).state == "failed"


def test_work_without_a_verdict_still_completes() -> None:
    """An operation with no ready task left has nothing to report against."""
    assert _supervised(None).state == STATE_DONE
