"""Carriers move frames; the protocol does not know which one it is on.

`QueueCarrier` is in-process (two queues and a pair of coroutines), `UnixSocket`
is a real socket with filesystem permissions as the trust boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from samurai_ai.services.rpc.protocol import MAX_FRAME_BYTES, ProtocolError, encode_line

Frame = dict[str, Any]
Handler = Callable[["Link"], Awaitable[None]]


class Link:
    """One connected peer: read frames, write frames, close."""

    async def read(self) -> Frame | None:
        raise NotImplementedError

    async def write(self, frame: Frame) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    @property
    def uid(self) -> int | None:
        return None


class QueueLink(Link):
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[Frame | None] = asyncio.Queue()
        self.outbound: asyncio.Queue[Frame | None] = asyncio.Queue()

    async def read(self) -> Frame | None:
        return await self.inbound.get()

    async def write(self, frame: Frame) -> None:
        await self.outbound.put(frame)

    async def close(self) -> None:
        await self.inbound.put(None)
        await self.outbound.put(None)


class QueueCarrier:
    """In-process carrier: a client link and a server link glued back to back."""

    def __init__(self) -> None:
        self.client = QueueLink()
        self.server = QueueLink()
        self._glued = False

    def _glue(self) -> None:
        if self._glued:
            return

        async def pump(source: QueueLink, target: QueueLink) -> None:
            while True:
                frame = await source.outbound.get()
                await target.inbound.put(frame)
                if frame is None:
                    return

        self._pumps = [
            asyncio.create_task(pump(self.client, self.server)),
            asyncio.create_task(pump(self.server, self.client)),
        ]
        self._glued = True

    async def accept(self, handler: Handler) -> asyncio.Task[None]:
        self._glue()
        return asyncio.ensure_future(handler(self.server))

    @property
    def link(self) -> Link:
        self._glue()
        return self.client

    async def stop(self) -> None:
        for task in getattr(self, "_pumps", []):
            task.cancel()


class SocketLink(Link):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._uid: int | None = None
        raw = writer.get_extra_info("socket")
        if isinstance(raw, socket.socket):
            option = getattr(socket, "SO_PEERCRED", None)
            if option is not None:
                try:
                    # struct ucred: pid, uid, gid — the uid is bytes 4..8
                    credential = raw.getsockopt(socket.SOL_SOCKET, option, 12)
                    self._uid = int.from_bytes(credential[4:8], "little")
                except OSError:  # pragma: no cover - platform quirk
                    self._uid = None

    @property
    def uid(self) -> int | None:
        return self._uid

    async def read(self) -> Frame | None:
        try:
            line = await self._reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
        except asyncio.LimitOverrunError as exc:  # pragma: no cover - flood guard
            raise ProtocolError("frame too large") from exc
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolError("frame too large")
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"frame is not JSON: {exc}") from exc
        if not isinstance(frame, dict):
            raise ProtocolError("frame must be an object")
        return frame

    async def write(self, frame: Frame) -> None:
        self._writer.write(encode_line(frame))
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:  # pragma: no cover - already gone
            pass


class UnixSocketCarrier:
    """A unix socket only the owner can connect to: 0600 in the state dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, handler: Handler) -> None:
        if len(str(self.path)) > 100:
            raise ProtocolError(
                f"socket path is {len(str(self.path))} bytes; AF_UNIX allows about "
                f"104 — use a shorter --socket"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()

        async def connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            link = SocketLink(reader, writer)
            try:
                await handler(link)
            finally:
                await link.close()

        self._server = await asyncio.start_unix_server(
            connected, path=str(self.path), limit=MAX_FRAME_BYTES
        )
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        if self.path.exists():
            self.path.unlink(missing_ok=True)


__all__ = ["Link", "QueueCarrier", "QueueLink", "UnixSocketCarrier"]
