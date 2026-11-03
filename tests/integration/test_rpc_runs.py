"""The registry keeps a run alive past its caller and stops it on request."""

import asyncio
import threading
from typing import Any

from samurai_ai.services.rpc.runs import RunBusy, RunRegistry


def test_a_run_outlives_the_coroutine_that_started_it() -> None:
    async def scenario() -> None:
        seen: list[str] = []
        events: list[str] = []

        async def work(handle) -> None:
            seen.append(handle.id)
            for _ in range(20):
                if handle.stop_event.is_set():
                    return
                await asyncio.sleep(0.01)

        def journal(_op: str, event_type: str, _payload: dict[str, Any]) -> None:
            events.append(event_type)

        registry = RunRegistry(work, journal)
        handle = registry.start("op_1", ["m"], None, "peer#1")
        await asyncio.sleep(0.02)
        assert registry.active("op_1") is handle
        assert seen == [handle.id]
        await asyncio.sleep(0.4)
        assert handle.state == "done"
        assert events == ["agent.run.start", "agent.run.finish"]
        await registry.drain()

    asyncio.run(scenario())


def test_a_second_run_on_the_same_operation_is_refused() -> None:
    async def scenario() -> None:
        async def work(handle) -> None:
            await asyncio.sleep(0.2)

        registry = RunRegistry(work, lambda *_: None)
        registry.start("op_1", ["m"], None, "p")
        try:
            registry.start("op_1", ["m"], None, "p")
        except RunBusy as exc:
            assert "in flight" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("the second run was allowed")
        # a different operation is not blocked
        registry.start("op_2", ["m"], None, "p")
        await registry.drain()

    asyncio.run(scenario())


def test_cancelling_sets_the_stop_event_and_lands_as_cancelled() -> None:
    async def scenario() -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def work(handle) -> None:
            for _ in range(100):
                if handle.stop_event.is_set():
                    return
                await asyncio.sleep(0.01)

        registry = RunRegistry(
            work, lambda _op, event, payload: events.append((event, payload))
        )
        handle = registry.start("op_1", ["m"], None, "p")
        await asyncio.sleep(0.05)
        registry.cancel(handle.id)
        assert handle.stop_event.is_set()
        await asyncio.sleep(0.2)
        assert handle.state == "cancelled"
        assert events[-1][1]["state"] == "cancelled"
        await registry.drain()

    asyncio.run(scenario())


def test_a_failing_run_does_not_take_the_registry_down() -> None:
    async def scenario() -> None:
        async def work(_handle) -> None:
            raise RuntimeError("boom")

        registry = RunRegistry(work, lambda *_: None)
        handle = registry.start("op_1", ["m"], None, "p")
        await asyncio.sleep(0.05)
        assert handle.state == "failed" and handle.detail == "boom"
        assert registry.active("op_1") is None
        await registry.drain()

    asyncio.run(scenario())


def test_cancel_of_an_unknown_run_raises() -> None:
    async def scenario() -> None:
        registry = RunRegistry(lambda h: asyncio.sleep(0), lambda *_: None)
        try:
            registry.cancel("run_nope")
        except KeyError:
            return
        raise AssertionError("cancel of nothing")  # pragma: no cover

    asyncio.run(scenario())


def test_stop_event_is_a_thread_event_for_the_worker_pool() -> None:
    async def scenario() -> None:
        registry = RunRegistry(lambda h: asyncio.sleep(0.05), lambda *_: None)
        handle = registry.start("op_1", ["m"], None, "p")
        assert isinstance(handle.stop_event, threading.Event)
        await registry.drain()

    asyncio.run(scenario())
