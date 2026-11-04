"""The server over the in-process carrier: auth, read-only, events."""

import asyncio
from typing import Any

from samurai_ai.domain.access import Principal, Role
from samurai_ai.services.rpc.carriers import QueueCarrier
from samurai_ai.services.rpc.peer import Peer, PeerKind
from samurai_ai.services.rpc.protocol import PROTOCOL_VERSION
from samurai_ai.services.rpc.server import EventFeed, RpcServer


def make_server(events: list[tuple[int, str, dict[str, Any]]] | None = None) -> RpcServer:
    rows = events if events is not None else []
    feed = EventFeed(lambda operation_id, seq, limit: [r for r in rows if r[0] > seq][:limit])
    server = RpcServer(feed)

    async def echo(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"echo": params.get("text")}

    async def write(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"wrote": True}

    async def subscribe(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"operation_id": params["operation_id"], "from_seq": params.get("from_seq", 0)}

    server.register("local.echo", echo, path="operation.show")
    server.register("agent.answer", write, write=True)
    server.register("events.subscribe", subscribe)
    return server


async def call(link, method: str, params: dict[str, Any] | None = None, id: int = 1):
    await link.write({"id": id, "method": method, "params": params or {}})
    return await link.read()


async def handshake(link, *, read_only: bool = False, client: str = "test") -> dict:
    await link.write({"hello": PROTOCOL_VERSION, "client": client, "read_only": read_only})
    return await link.read()


def test_a_read_method_answers() -> None:
    async def scenario() -> None:
        server = make_server()
        await server.start()
        carrier = QueueCarrier()
        task = await carrier.accept(server.serve)
        await handshake(carrier.link)
        response = await call(carrier.link, "local.echo", {"text": "hi"})
        assert response["result"] == {"echo": "hi"}
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())


def test_a_read_only_peer_cannot_write() -> None:
    async def scenario() -> None:
        server = make_server()
        await server.start()
        carrier = QueueCarrier()
        task = await carrier.accept(server.serve)
        await handshake(carrier.link, read_only=True)
        response = await call(carrier.link, "agent.answer")
        assert response["error"]["code"] == "read-only"
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())


def test_a_viewer_peer_is_refused_a_write_by_role() -> None:
    async def scenario() -> None:
        server = make_server()
        await server.start()
        carrier = QueueCarrier()
        peer = Peer(
            principal=Principal(subject="v", role=Role.VIEWER),
            kind=PeerKind.IN_PROCESS,
        )
        task = await carrier.accept(lambda link: server.serve(link, peer=peer))
        await handshake(carrier.link)
        assert (await call(carrier.link, "agent.answer"))["error"]["code"] == "forbidden"
        # reads still work
        assert (await call(carrier.link, "local.echo", {"text": "x"}))["result"]
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())


def test_an_unknown_method_is_reported() -> None:
    async def scenario() -> None:
        server = make_server()
        await server.start()
        carrier = QueueCarrier()
        task = await carrier.accept(server.serve)
        await handshake(carrier.link)
        assert (await call(carrier.link, "nope.nope"))["error"]["code"] == "no-method"
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())


def test_subscribers_receive_new_events_and_can_resume() -> None:
    async def scenario() -> None:
        rows: list[tuple[int, str, dict[str, Any]]] = [(1, "old", {}), (2, "old2", {})]
        server = make_server(rows)
        await server.start()
        carrier = QueueCarrier()
        task = await carrier.accept(server.serve)
        await handshake(carrier.link)
        await call(carrier.link, "events.subscribe", {"operation_id": "op_1", "from_seq": 2})
        rows.append((3, "tool.run.completed", {"tool_id": "scan.nmap"}))
        await asyncio.sleep(0.5)
        # the welcome, the subscribe response, then the one new event only
        assert (await carrier.link.read())["event"] == "tool.run.completed"
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())


def test_a_bad_handshake_is_refused() -> None:
    async def scenario() -> None:
        server = make_server()
        await server.start()
        carrier = QueueCarrier()
        task = await carrier.accept(server.serve)
        await carrier.link.write({"hello": 99})
        assert "protocol mismatch" in (await carrier.link.read())["error"]["message"]
        await carrier.link.close()
        await task
        await server.aclose()
        await carrier.stop()

    asyncio.run(scenario())
