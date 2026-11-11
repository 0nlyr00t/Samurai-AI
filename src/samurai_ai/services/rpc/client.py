"""Client side of the protocol: one connection, typed calls, event stream.

The TUI and the CLI both attach through this, so "another terminal" and "another
frontend" are the same thing to the server.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from samurai_ai.services.rpc.carriers import Link, SocketLink
from samurai_ai.services.rpc.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ErrorBody,
    ProtocolError,
)


class RpcFailure(RuntimeError):
    def __init__(self, error: ErrorBody) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.code = error.code


class Client:
    def __init__(self, link: Link, welcome: dict[str, Any]) -> None:
        self.link = link
        self.welcome = welcome
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader = asyncio.ensure_future(self._read_loop())
        self.events: asyncio.Queue[Any] = asyncio.Queue()

    @property
    def peer(self) -> str:
        return str(self.welcome.get("peer", ""))

    @classmethod
    async def connect(
        cls, path: Path, *, client: str = "cli", read_only: bool = False
    ) -> Client:
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(path), limit=MAX_FRAME_BYTES
            )
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise RpcFailure(
                ErrorBody("no-daemon", f"no live daemon on {path}: {exc}")
            ) from exc
        link = SocketLink(reader, writer)
        await link.write(
            {"hello": PROTOCOL_VERSION, "client": client, "read_only": read_only}
        )
        welcome = await link.read()
        if welcome is None:
            raise RpcFailure(ErrorBody("handshake", "the daemon hung up at once"))
        if "error" in welcome:
            raise RpcFailure(ErrorBody(**welcome["error"]))
        return cls(link, welcome)

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str = "",
    ) -> Any:
        request_id = next(self._ids)
        if idempotency_key:
            params = {**(params or {}), "idempotency_key": idempotency_key}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.link.write({"id": request_id, "method": method, "params": params or {}})
        return await future

    async def subscribe(self, operation_id: str, from_seq: int = 0) -> int:
        result = await self.call(
            "events.subscribe", {"operation_id": operation_id, "from_seq": from_seq}
        )
        return int(result["from_seq"])

    async def _read_loop(self) -> None:
        while True:
            try:
                frame = await self.link.read()
            except ProtocolError:
                frame = None
            if frame is None:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            RpcFailure(ErrorBody("closed", "the daemon hung up"))
                        )
                await self.events.put(None)
                return
            if "event" in frame or "resync" in frame:
                # a resync frame says how many events this peer missed; the
                # consumer re-reads a snapshot instead of pretending it is current
                await self.events.put(frame)
                continue
            waiting = self._pending.pop(int(frame.get("id", 0)), None)
            if waiting is None or waiting.done():
                continue
            future = waiting
            if "error" in frame:
                future.set_exception(RpcFailure(ErrorBody(**frame["error"])))
            else:
                future.set_result(frame.get("result"))

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            frame = await self.events.get()
            if frame is None:
                return
            yield frame

    async def close(self) -> None:
        await self.link.close()
        self._reader.cancel()


def format_event(frame: dict[str, Any]) -> str:
    payload = json.dumps(frame.get("payload") or {}, ensure_ascii=False, sort_keys=True)
    if len(payload) > 400:
        payload = payload[:397] + "..."
    return f"[{frame.get('seq')}] {frame.get('event')} {payload}"


__all__ = ["Client", "RpcFailure", "format_event"]
