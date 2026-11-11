"""A burst of events: peers stay current, memory stays bounded, input stays free."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.services.rpc.carriers import UnixSocketCarrier
from samurai_ai.services.rpc.client import Client
from samurai_ai.services.rpc.peer import Peer
from samurai_ai.services.rpc.server import PEER_QUEUE, EventFeed, RpcServer


@pytest.fixture
def short_dir() -> Path:
    directory = Path(tempfile.mkdtemp(dir="/tmp", prefix="samurai-soak-"))
    try:
        yield directory  # type: ignore[misc]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_burst_reaches_both_peers_and_stays_bounded(short_dir: Path) -> None:
    total = 1200
    rows: list[tuple[int, str, dict[str, Any]]] = []
    feed = EventFeed(lambda _op, seq, limit: [r for r in rows if r[0] > seq][:limit])
    server = RpcServer(feed)

    async def write(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": params.get("n")}

    async def subscribe(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"operation_id": params["operation_id"], "from_seq": params.get("from_seq", 0)}

    server.register("send.message", write, write=True)
    server.register("events.subscribe", subscribe)

    async def scenario() -> None:
        socket_path = short_dir / "s.sock"
        carrier = UnixSocketCarrier(socket_path)
        await server.start()
        await carrier.start(server.serve)
        first = await Client.connect(socket_path, client="one")
        second = await Client.connect(socket_path, client="two", read_only=True)
        await first.subscribe("op_1", 0)
        await second.subscribe("op_1", 0)
        for seq in range(1, total + 1):
            rows.append((seq, "noise", {"seq": seq}))
        # while the pump catches up the connection still answers a call
        answer = await asyncio.wait_for(first.call("send.message", {"n": 1}), 5)
        assert answer == {"ok": 1}
        await asyncio.sleep(1.5)
        # a consumer keeps reading: the queue never grows past its bound
        assert all(queue.qsize() <= PEER_QUEUE for queue in feed.queues.values())
        assert len(feed.queues) == 2
        await first.close()
        await second.close()
        await carrier.stop()
        await server.aclose()

    asyncio.run(scenario())
