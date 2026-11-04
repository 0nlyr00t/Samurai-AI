"""The RPC server: dispatch, authorization, event fan-out.

Method names are the CLI command paths (`agent.answer`, `operation.show`), so the
existing access table applies unchanged and a socket peer is judged like a CLI
invocation — the daemon adds transport, not a second policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from samurai_ai.domain.access import ForbiddenError, require
from samurai_ai.services.rpc.carriers import Link
from samurai_ai.services.rpc.peer import Peer, operator_peer
from samurai_ai.services.rpc.protocol import (
    PROTOCOL_VERSION,
    ErrorBody,
    Hello,
    ProtocolError,
    Request,
    Response,
    decode,
)

Handler = Callable[[Peer, dict[str, Any]], Awaitable[Any]]
EVENT_PUMP_SECONDS = 0.25


class MethodError(Exception):
    """A refusal the client sees as an error frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Method:
    handler: Handler
    path: str
    write: bool


class EventFeed:
    """Reads the journal forward per subscription; SQLite is the buffer."""

    def __init__(
        self, fetch: Callable[[str, int, int], list[tuple[int, str, dict[str, Any]]]]
    ) -> None:
        self.fetch = fetch
        self.subscriptions: dict[Link, dict[str, int]] = {}

    def subscribe(self, link: Link, operation_id: str, from_seq: int) -> None:
        self.subscriptions.setdefault(link, {})[operation_id] = from_seq

    def drop(self, link: Link) -> None:
        self.subscriptions.pop(link, None)

    async def pump(self, write: Callable[[Link, dict[str, Any]], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(EVENT_PUMP_SECONDS)
            for link, watched in list(self.subscriptions.items()):
                for operation_id, seq in list(watched.items()):
                    rows = self.fetch(operation_id, seq, 100)
                    for event_id, event_type, payload in rows:
                        frame = {
                            "event": event_type,
                            "seq": event_id,
                            "operation_id": operation_id,
                            "payload": payload,
                        }
                        with contextlib.suppress(ConnectionError):
                            await write(link, frame)
                        watched[operation_id] = event_id


class RpcServer:
    def __init__(self, feed: EventFeed) -> None:
        self.feed = feed
        self.methods: dict[str, Method] = {}
        self._links: dict[Link, Peer] = {}
        self._pump: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._pump is None:
            self._pump = asyncio.create_task(self.feed.pump(self._write))

    async def aclose(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None

    # -- registration -----------------------------------------------------
    def register(self, name: str, handler: Handler, *, write: bool = False,
                 path: str | None = None) -> None:
        if name in self.methods:
            raise ValueError(f"method already registered: {name}")
        self.methods[name] = Method(handler=handler, path=path or name, write=write)

    # -- connection lifecycle ---------------------------------------------
    async def serve(self, link: Link, *, peer: Peer | None = None) -> None:
        """Handle one peer until it disconnects."""
        try:
            hello = await self._handshake(link)
        except ProtocolError as exc:
            await self._write(link, {"error": {"code": "protocol", "message": str(exc)}})
            return
        resolved = peer or operator_peer(
            read_only=hello.read_only, client=hello.client
        )
        self._links[link] = resolved
        await self._write(link, {"welcome": PROTOCOL_VERSION, **resolved.as_frame()})
        try:
            while True:
                frame = await link.read()
                if frame is None:
                    return
                await self._handle_frame(link, resolved, frame)
        finally:
            self.feed.drop(link)
            self._links.pop(link, None)

    async def _handshake(self, link: Link) -> Hello:
        frame = await link.read()
        if frame is None:
            raise ProtocolError("peer closed before the handshake")
        decoded = decode(frame)
        if not isinstance(decoded, Hello):
            raise ProtocolError("the first frame must be a hello")
        return decoded

    # -- dispatch ---------------------------------------------------------
    async def _handle_frame(self, link: Link, peer: Peer, frame: dict[str, Any]) -> None:
        try:
            request = decode(frame)
        except ProtocolError as exc:
            refusal = Response(id=0, error=ErrorBody("protocol", str(exc)))
            await self._write(link, refusal.as_frame())
            return
        if isinstance(request, Hello):
            return
        response = await self._call(link, peer, request)
        with contextlib.suppress(ConnectionError):
            await link.write(response.as_frame())

    async def _call(self, link: Link, peer: Peer, request: Request) -> Response:
        method = self.methods.get(request.method)
        if method is None:
            return Response(request.id, error=ErrorBody("no-method", request.method))
        try:
            require(peer.principal, method.path)
            if method.write and peer.read_only:
                raise MethodError("read-only", f"{method.path} needs a writing peer")
            result = await method.handler(peer, request.params)
        except ForbiddenError as exc:
            return Response(request.id, error=ErrorBody("forbidden", str(exc)))
        except MethodError as exc:
            return Response(request.id, error=ErrorBody(exc.code, str(exc)))
        except Exception as exc:  # noqa: BLE001 - the peer must see something
            return Response(request.id, error=ErrorBody(type(exc).__name__, str(exc)))
        if request.method == "events.subscribe" and isinstance(result, dict):
            self.feed.subscribe(
                link,
                str(result["operation_id"]),
                int(result.get("from_seq") or 0),
            )
        return Response(request.id, result=result)

    @staticmethod
    async def _write(link: Link, frame: dict[str, Any]) -> None:
        await link.write(frame)


__all__ = ["EventFeed", "Method", "MethodError", "RpcServer"]
