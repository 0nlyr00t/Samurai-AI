"""A slow peer must not stall the others, and must learn what it missed."""

from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.services.rpc.server import PEER_QUEUE, EventFeed


def test_a_full_queue_drops_the_oldest_and_sends_a_resync() -> None:
    async def scenario() -> None:
        feed = EventFeed(lambda _op, _seq, _limit: [])
        written: list[dict[str, Any]] = []

        async def write(_link: object, frame: dict[str, Any]) -> None:
            written.append(frame)

        link = object()
        feed.subscribe(link, "op_1", 0)
        feed._ensure_writer(link, write)  # type: ignore[arg-type]
        for seq in range(1, PEER_QUEUE * 2):
            feed.offer(link, {"event": "noise", "seq": seq, "operation_id": "op_1", "payload": {}})
        assert feed.dropped[link] > 0
        for _ in range(300):
            if len(written) >= 2:
                break
            await asyncio.sleep(0.01)
        assert "resync" in written[0]
        assert written[0]["resync"] > 0
        # what survives is the newest frames, not the oldest
        assert written[1]["seq"] > 1
        feed.drop(link)

    asyncio.run(scenario())


def test_one_slow_peer_does_not_hold_up_another() -> None:
    async def scenario() -> None:
        feed = EventFeed(lambda _op, _seq, _limit: [])
        fast_written: list[int] = []

        async def fast(_link: object, frame: dict[str, Any]) -> None:
            fast_written.append(int(frame["seq"]))

        slow_link, fast_link = object(), object()
        feed.subscribe(slow_link, "op_1", 0)
        feed.subscribe(fast_link, "op_1", 0)
        # the slow peer's writer never runs: its queue just fills
        feed._ensure_writer(fast_link, fast)  # type: ignore[arg-type]
        for seq in range(1, 20):
            for link in (slow_link, fast_link):
                feed.offer(link, {"event": "noise", "seq": seq, "operation_id": "op_1",
                                  "payload": {}})
        for _ in range(200):
            if len(fast_written) >= 19:
                break
            await asyncio.sleep(0.01)
        assert len(fast_written) == 19
        feed.drop(slow_link)
        feed.drop(fast_link)

    asyncio.run(scenario())
