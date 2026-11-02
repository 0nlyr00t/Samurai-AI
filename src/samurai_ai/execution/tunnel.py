"""Local TCP tunnels to in-scope targets through the scope policy.

Some attacking tools only speak their own protocol (ssh, RDP, SMB, ADB) and
cannot be pointed at an HTTP proxy. A tunnel gives such a tool a localhost port
that stands for one target: the verdict is taken once, before any byte flows,
and the connection is then plain bytes — same policy, same EgressEvents, no
protocol rewriting.

The listener is bound to the host loopback and lives only for one tool run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.domain.scope import ScopeContract, ScopeDecisionReason
from samurai_ai.execution.egress import (
    VERDICT_ALLOWED,
    VERDICT_DENIED_PORT,
    VERDICT_DENIED_SCOPE,
    EgressEvent,
)
from samurai_ai.execution.http import Resolver, default_resolver

_READ_CHUNK = 65536
_MAX_CONNECTIONS = 8
_IDLE_TIMEOUT = 30.0
_DEFAULT_PORTS = frozenset({22, 80, 443, 445, 3389, 5555, 5985, 5986})


@dataclass(frozen=True)
class TunnelEndpoint:
    """Where the tool must connect, as seen from inside the sandbox."""

    host: str
    port: int
    url: str


def scope_url(host: str) -> str:
    """A scope target string. The scheme is cosmetic (scope ignores ports), and
    IPv6 literals need brackets. `http` is used because the scope parser only
    understands http(s) URLs."""
    literal = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{literal}/"


class ScopedTunnel:
    def __init__(
        self,
        scope: ScopeContract,
        host: str,
        port: int,
        *,
        allowed_ports: frozenset[int] = _DEFAULT_PORTS,
        resolver: Resolver = default_resolver,
        bind_host: str = "127.0.0.1",
        idle_timeout: float = _IDLE_TIMEOUT,
        max_connections: int = _MAX_CONNECTIONS,
        on_event: Callable[[EgressEvent], None] | None = None,
    ) -> None:
        self.scope = scope
        self.target_host = host
        self.target_port = port
        self.allowed_ports = allowed_ports
        self.resolver = resolver
        self.bind_host = bind_host
        self.idle_timeout = idle_timeout
        self.on_event = on_event
        self.events: list[EgressEvent] = []
        self.bytes_sent = 0
        self.bytes_received = 0
        self._server: asyncio.Server | None = None
        self._semaphore = asyncio.Semaphore(max_connections)

    @property
    def port(self) -> int:
        if self._server is None:
            raise ToolExecutionError("tunnel is not started")
        return int(self._server.sockets[0].getsockname()[1])

    def endpoint(self, host_alias: str) -> TunnelEndpoint:
        """`host_alias` is how the sandbox names the host (host.docker.internal)."""
        return TunnelEndpoint(
            host=host_alias,
            port=self.port,
            url=f"{host_alias}:{self.port}",
        )

    def verdict(self) -> str:
        if self.target_port not in self.allowed_ports:
            return VERDICT_DENIED_PORT
        decision = self.scope.evaluate(scope_url(self.target_host))
        if not decision.allowed:
            return VERDICT_DENIED_SCOPE
        try:
            addresses = self.resolver(self.target_host)
        except ToolExecutionError:
            return VERDICT_DENIED_SCOPE
        for address in addresses:
            if self.scope.evaluate(address).reason == ScopeDecisionReason.EXCLUDED:
                return VERDICT_DENIED_SCOPE
        return VERDICT_ALLOWED

    def check(self) -> None:
        verdict = self.verdict()
        if verdict != VERDICT_ALLOWED:
            self._emit(verdict)
            raise ToolExecutionError(
                f"tunnel to {self.target_host}:{self.target_port} denied ({verdict})"
            )

    async def start(self) -> TunnelEndpoint:
        self.check()
        self._server = await asyncio.start_server(
            self._handle, host=self.bind_host, port=0
        )
        return self.endpoint("host.docker.internal")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _emit(self, verdict: str, sent: int = 0, received: int = 0) -> None:
        event = EgressEvent(
            host=self.target_host,
            port=self.target_port,
            verdict=verdict,
            bytes_sent=sent,
            bytes_received=received,
        )
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with self._semaphore:
            upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
            try:
                addresses = self.resolver(self.target_host)
                upstream = await asyncio.open_connection(
                    addresses[0], self.target_port
                )
            except (ToolExecutionError, OSError):
                self._emit(VERDICT_DENIED_SCOPE)
                writer.close()
                return
            # the connection is up: audit it now, volume follows on the totals
            self._emit(VERDICT_ALLOWED)
            try:
                sent, received = await self._pump(reader, writer, *upstream)
                self.bytes_sent += sent
                self.bytes_received += received
            except (ToolExecutionError, OSError):
                pass
            finally:
                writer.close()
                upstream[1].close()

    async def _pump(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        counters = {"sent": 0, "received": 0}

        async def pump(
            source: asyncio.StreamReader,
            target: asyncio.StreamWriter,
            key: str,
        ) -> None:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        source.read(_READ_CHUNK), timeout=self.idle_timeout
                    )
                except TimeoutError:
                    return  # idle: the tool is done with this connection
                if not chunk:
                    return
                target.write(chunk)
                await target.drain()
                counters[key] += len(chunk)

        forward = asyncio.create_task(pump(client_reader, upstream_writer, "sent"))
        backward = asyncio.create_task(pump(upstream_reader, client_writer, "received"))
        _, pending = await asyncio.wait(
            {forward, backward}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return counters["sent"], counters["received"]


__all__ = ["ScopedTunnel", "TunnelEndpoint", "scope_url"]
