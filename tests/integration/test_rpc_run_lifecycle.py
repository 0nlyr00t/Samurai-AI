"""Over the socket: a dropped client must not take the run with it."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.services.rpc.carriers import UnixSocketCarrier
from samurai_ai.services.rpc.client import Client, RpcFailure
from samurai_ai.services.rpc.peer import Peer
from samurai_ai.services.rpc.runs import RunBusy, RunHandle, RunRegistry
from samurai_ai.services.rpc.server import EventFeed, MethodError, RpcServer


@pytest.fixture
def short_dir():
    directory = Path(tempfile.mkdtemp(dir="/tmp", prefix="samurai-run-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class Stand:
    def __init__(self, socket_path: Path) -> None:
        self.rows: list[tuple[int, str, dict[str, Any]]] = []
        self._next = 0

        async def work(handle: RunHandle) -> None:
            for _ in range(200):
                if handle.stop_event.is_set():
                    return
                await asyncio.sleep(0.01)

        def journal(operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
            self._next += 1
            self.rows.append((self._next, event_type, {**payload, "op": operation_id}))

        self.runs = RunRegistry(work, journal)
        feed = EventFeed(
            lambda _op, seq, limit: [row for row in self.rows if row[0] > seq][:limit]
        )
        self.server = RpcServer(feed)

        async def start(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
            try:
                return self.runs.start("op_1", ["m"], None, "p").as_frame()
            except RunBusy as exc:
                raise MethodError("busy", str(exc)) from exc

        async def listing(_peer: Peer, _params: dict[str, Any]) -> list[dict[str, Any]]:
            return self.runs.listing("op_1")

        async def cancel(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
            return self.runs.cancel(str(params["run_id"])).as_frame()

        async def subscribe(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
            return {"operation_id": "op_1", "from_seq": params.get("from_seq", 0)}

        self.server.register("events.subscribe", subscribe)
        self.server.register("run.start", start, write=True)
        self.server.register("run.list", listing)
        self.server.register("run.cancel", cancel, write=True)
        self.carrier = UnixSocketCarrier(socket_path)

    async def open(self) -> None:
        await self.server.start()
        await self.carrier.start(self.server.serve)

    async def close(self) -> None:
        await self.runs.drain()
        await self.carrier.stop()
        await self.server.aclose()


def test_a_run_survives_the_client_that_started_it(short_dir: Path) -> None:
    async def scenario() -> None:
        stand = Stand(short_dir / "s.sock")
        await stand.open()
        first = await Client.connect(stand.carrier.path, client="one")
        started = await first.call("run.start")
        await asyncio.sleep(0.05)
        await first.close()
        await asyncio.sleep(0.1)
        second = await Client.connect(stand.carrier.path, client="two")
        listing = await second.call("run.list")
        assert [row["state"] for row in listing] == ["running"]
        # the new client can see the tail and stop it
        await second.subscribe("op_1", 0)
        await second.call("run.cancel", {"run_id": started["run_id"]})
        seen: list[str] = []
        for _ in range(20):  # backlog first, then the finish
            frame = await asyncio.wait_for(second.events.get(), 5)
            seen.append(frame["event"])
            if frame["event"] == "agent.run.finish":
                break
        assert seen[-1] == "agent.run.finish"
        await asyncio.sleep(0.2)
        assert (await second.call("run.list"))[0]["state"] == "cancelled"
        await second.close()
        await stand.close()

    asyncio.run(scenario())


def test_a_second_run_is_refused_with_busy(short_dir: Path) -> None:
    async def scenario() -> None:
        stand = Stand(short_dir / "s.sock")
        await stand.open()
        client = await Client.connect(stand.carrier.path, client="one")
        await client.call("run.start")
        try:
            await client.call("run.start")
        except RpcFailure as exc:
            assert exc.code == "busy"
        else:  # pragma: no cover
            raise AssertionError("two runs at once")
        await client.close()
        await stand.close()

    asyncio.run(scenario())
