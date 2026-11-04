"""The socket carrier: permissions, refusal, cursor, two clients at once."""

import asyncio
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest

from samurai_ai.services.rpc.carriers import UnixSocketCarrier
from samurai_ai.services.rpc.client import Client, RpcFailure
from samurai_ai.services.rpc.peer import Peer
from samurai_ai.services.rpc.server import EventFeed, MethodError, RpcServer


@pytest.fixture
def short_dir() -> Path:
    """AF_UNIX paths are capped at ~104 bytes; pytest's tmp_path is longer."""
    directory = Path(tempfile.mkdtemp(dir="/tmp", prefix="samurai-rpc-"))
    yield_ = directory
    try:
        yield yield_
    finally:
        shutil.rmtree(yield_, ignore_errors=True)


def build(socket_path: Path, rows: list[tuple[int, str, dict[str, Any]]]) -> tuple[
    RpcServer, UnixSocketCarrier
]:
    server = RpcServer(EventFeed(lambda _op, seq, limit: [r for r in rows if r[0] > seq][:limit]))

    async def echo(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        return {"echo": params.get("text")}

    async def write(_peer: Peer, _params: dict[str, Any]) -> dict[str, Any]:
        return {"wrote": True}

    async def subscribe(_peer: Peer, params: dict[str, Any]) -> dict[str, Any]:
        if not params.get("operation_id"):
            raise MethodError("missing", "operation_id is required")
        return {"operation_id": params["operation_id"], "from_seq": params.get("from_seq", 0)}

    server.register("operation.show", echo)
    server.register("send.message", write, write=True)
    server.register("events.subscribe", subscribe)
    return server, UnixSocketCarrier(socket_path)


def test_the_socket_is_private_and_the_handshake_is_required(short_dir: Path) -> None:
    async def scenario() -> None:
        socket_path = short_dir / "run" / "s.sock"
        server, carrier = build(socket_path, [])
        await server.start()
        await carrier.start(server.serve)
        mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert mode == 0o600
        client = await Client.connect(socket_path)
        assert await client.call("operation.show", {"text": "hi"}) == {"echo": "hi"}
        # a raw writer that skips hello gets nothing but a protocol error
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(b'{"id":1,"method":"operation.show"}\n')
        await writer.drain()
        line = await reader.readuntil(b"\n")
        assert b"hello" in line
        writer.close()
        await client.close()
        await carrier.stop()
        await server.aclose()

    asyncio.run(scenario())


def test_a_read_only_client_cannot_write(short_dir: Path) -> None:
    async def scenario() -> None:
        socket_path = short_dir / "s.sock"
        server, carrier = build(socket_path, [])
        await server.start()
        await carrier.start(server.serve)
        client = await Client.connect(socket_path, read_only=True)
        with_pytest = await client.call("operation.show", {"text": "x"})
        assert with_pytest == {"echo": "x"}
        try:
            await client.call("send.message", {})
        except RpcFailure as exc:
            assert exc.code == "read-only"
        else:  # pragma: no cover - the point of the test
            raise AssertionError("a read-only peer wrote")
        await client.close()
        await carrier.stop()
        await server.aclose()

    asyncio.run(scenario())


def test_two_clients_and_a_cursor_resume(short_dir: Path) -> None:
    async def scenario() -> None:
        rows: list[tuple[int, str, dict[str, Any]]] = [(1, "operation.created", {})]
        socket_path = short_dir / "s.sock"
        server, carrier = build(socket_path, rows)
        await server.start()
        await carrier.start(server.serve)
        first = await Client.connect(socket_path, client="one", read_only=True)
        second = await Client.connect(socket_path, client="two", read_only=True)
        assert await first.subscribe("op_1", 1) == 1
        assert await second.subscribe("op_1", 1) == 1
        rows.append((2, "operator.message", {"text": "hello"}))
        seen_first = await asyncio.wait_for(first.events.get(), 5)
        seen_second = await asyncio.wait_for(second.events.get(), 5)
        assert seen_first["seq"] == 2 and seen_second["seq"] == 2
        # a client that drops and comes back asks to resume from where it was
        await first.close()
        again = await Client.connect(socket_path, client="one", read_only=True)
        assert await again.subscribe("op_1", 2) == 2
        rows.append((3, "operator.message", {"text": "later"}))
        assert (await asyncio.wait_for(again.events.get(), 5))["seq"] == 3
        # the dead client's subscription does not wedge the feed
        assert len(server.feed.subscriptions) == 2
        await again.close()
        await second.close()
        await carrier.stop()
        await server.aclose()

    asyncio.run(scenario())


def test_a_dropped_client_is_forgotten(short_dir: Path) -> None:
    async def scenario() -> None:
        socket_path = short_dir / "s.sock"
        server, carrier = build(socket_path, [])
        await server.start()
        await carrier.start(server.serve)
        client = await Client.connect(socket_path, read_only=True)
        await client.subscribe("op_1", 0)
        assert len(server.feed.subscriptions) == 1
        await client.close()
        for _ in range(40):
            if not server.feed.subscriptions:
                break
            await asyncio.sleep(0.05)
        assert server.feed.subscriptions == {}
        await carrier.stop()
        await server.aclose()

    asyncio.run(scenario())


def test_no_daemon_is_a_clean_refusal(short_dir: Path) -> None:
    async def scenario() -> None:
        try:
            await Client.connect(short_dir / "missing.sock")
        except RpcFailure as exc:
            assert exc.code == "no-daemon"
        else:  # pragma: no cover
            raise AssertionError("connected to nothing")

    asyncio.run(scenario())
