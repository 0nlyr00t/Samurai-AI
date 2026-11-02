"""Scoped egress forward proxy for sandbox network_mode "scoped".

A per-run, token-authenticated forward proxy bound to the host loopback on
an ephemeral port. Sandboxed containers reach it via the host gateway
(``host.docker.internal``) with HTTP(S)_PROXY env vars; every request needs
``Proxy-Authorization: Bearer <token>`` (or Basic with the token as the
password, which is what curl/wget send for proxy URLs with userinfo).

Two entrances share one policy: HTTP (plain forwarding and CONNECT tunnels)
and SOCKS5 CONNECT-only (RFC 1928/1929, username ``samurai``, password = the
per-run token) for tools that take nothing but a SOCKS URL. SOCKS5 is
recognized by its 0x05 greeting byte; it runs the same ``_verdict`` and emits
the same EgressEvents, and UDP ASSOCIATE is not implemented.

Policy is deterministic from the operation ScopeContract: the target host is
evaluated against domain/wildcard/URL/IP rules, the hostname is resolved once
and every resolved address is checked against exclusions, and the upstream
connection is made to a pinned validated address (no DNS TOCTOU, same
pipeline as the HTTP broker). Only ports 80/443 are allowed by default.
CONNECT tunnels only see host:port — the verdict is made on the host before
any byte of payload flows. Every request is reported as an EgressEvent
(host, port, verdict, byte counts) — never the content.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_hex

from samurai_ai.domain.execution import ToolExecutionError
from samurai_ai.domain.scope import ScopeContract, ScopeDecisionReason
from samurai_ai.execution.http import Resolver, default_resolver

DEFAULT_ALLOWED_PORTS = frozenset({80, 443})
_MAX_HEADER_BYTES = 65536
_DEFAULT_MAX_RESPONSE_BYTES = 1 << 20
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_CONNECTIONS = 16

VERDICT_ALLOWED = "allowed"
VERDICT_DENIED_AUTH = "denied-auth"
VERDICT_DENIED_SCOPE = "denied-scope"
VERDICT_DENIED_PORT = "denied-port"
VERDICT_DENIED_PROTOCOL = "denied-protocol"

_SOCKS5_VERSION = 5
_SOCKS5_AUTH_VERSION = 1
_SOCKS5_METHOD_NO_AUTH = 0
_SOCKS5_METHOD_USERPASS = 2
_SOCKS5_METHOD_NONE_ACCEPTABLE = 0xFF
_SOCKS5_CMD_CONNECT = 1
_SOCKS5_ATYP_IPV4 = 1
_SOCKS5_ATYP_DOMAIN = 3
_SOCKS5_ATYP_IPV6 = 4
_SOCKS5_REP_SUCCESS = 0
_SOCKS5_REP_NOT_ALLOWED = 2
_SOCKS5_REP_UNREACHABLE = 3
_SOCKS5_REP_REFUSED = 5
_SOCKS5_REP_CMD_UNSUPPORTED = 7
_SOCKS5_REP_ATYP_UNSUPPORTED = 8
_SOCKS5_USERNAME = "samurai"


@dataclass(frozen=True)
class EgressEvent:
    host: str
    port: int
    verdict: str
    bytes_sent: int = 0
    bytes_received: int = 0


def generate_proxy_token() -> str:
    return token_hex(16)


def check_proxy_authorization(header: str | None, token: str) -> bool:
    if not header:
        return False
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() == "bearer":
        return credentials.strip() == token
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(credentials.strip()).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return False
        _, _, password = decoded.partition(":")
        return password == token
    return False


def parse_request_target(
    method: str, target: str
) -> tuple[str, str, int, str]:
    """Return (scheme, host, port, path) for CONNECT and absolute-URI forms."""
    if method == "CONNECT":
        if target.startswith("["):
            host, separator, rest = target[1:].partition("]")
            if not separator or not rest.startswith(":"):
                raise ToolExecutionError(f"invalid CONNECT target: {target!r}")
            raw_port = rest[1:]
        else:
            host, separator, raw_port = target.partition(":")
        if not separator or not host:
            raise ToolExecutionError(f"invalid CONNECT target: {target!r}")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ToolExecutionError(f"invalid CONNECT port: {target!r}") from exc
        return "https", host, port, ""
    parts = urllib.parse.urlsplit(target)
    if parts.scheme.lower() != "http":
        raise ToolExecutionError(
            f"only plain http absolute-URI relay is supported: {target!r}"
        )
    if not parts.hostname:
        raise ToolExecutionError(f"absolute-URI target requires a host: {target!r}")
    port = parts.port or 80
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    return "http", parts.hostname, port, path


@dataclass
class _Counters:
    sent: int = 0
    received: int = 0


class EgressProxy:
    def __init__(
        self,
        scope: ScopeContract,
        token: str,
        *,
        bind_host: str = "127.0.0.1",
        port: int = 0,
        allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        timeout: float = _DEFAULT_TIMEOUT,
        max_connections: int = _DEFAULT_MAX_CONNECTIONS,
        resolver: Resolver = default_resolver,
        on_event: Callable[[EgressEvent], None] | None = None,
        extra_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self.scope = scope
        self.token = token
        self.bind_host = bind_host
        self.port = port
        self.allowed_ports = allowed_ports
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout
        self.resolver = resolver
        self.on_event = on_event
        self.extra_hosts = extra_hosts
        self.events: list[EgressEvent] = []
        self._server: asyncio.Server | None = None
        self._semaphore = asyncio.Semaphore(max_connections)

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise ToolExecutionError("egress proxy is not started")
        socket = self._server.sockets[0]
        host, port = socket.getsockname()[:2]
        return str(host), int(port)

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(
            self._handle, host=self.bind_host, port=self.port
        )
        return self.address

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _emit(self, event: EgressEvent) -> None:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    def _in_extra_hosts(self, host: str) -> bool:
        if not self.extra_hosts:
            return False
        candidate = host.lower().rstrip(".")
        for entry in self.extra_hosts:
            if entry.startswith("*."):
                base = entry[2:]
                if candidate == base or candidate.endswith(f".{base}"):
                    return True
            elif candidate == entry:
                return True
        return False

    def _verdict(self, host: str, port: int, scheme: str) -> str:
        if port not in self.allowed_ports:
            return VERDICT_DENIED_PORT
        if self._in_extra_hosts(host):
            # Third-party sources of a passive tool: allowed by the signed
            # profile, still resolved through the pinned resolver and audited.
            try:
                self.resolver(host)
            except ToolExecutionError:
                return VERDICT_DENIED_SCOPE
            return VERDICT_ALLOWED
        decision = self.scope.evaluate(_scope_url(host, scheme))
        if not decision.allowed:
            return VERDICT_DENIED_SCOPE
        try:
            addresses = self.resolver(host)
        except ToolExecutionError:
            return VERDICT_DENIED_SCOPE
        for address in addresses:
            if self.scope.evaluate(address).reason == ScopeDecisionReason.EXCLUDED:
                return VERDICT_DENIED_SCOPE
        return VERDICT_ALLOWED

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async with self._semaphore:
            try:
                await asyncio.wait_for(self._dispatch(reader, writer), self.timeout)
            except (TimeoutError, ToolExecutionError, ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass

    async def _dispatch(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        first = await reader.readexactly(1)
        if first[0] == _SOCKS5_VERSION:
            await self._dispatch_socks5(reader, writer)
            return
        request = first + await reader.readuntil(b"\r\n\r\n")
        if len(request) > _MAX_HEADER_BYTES:
            raise ToolExecutionError("proxy request headers too large")
        try:
            header_text = request.decode("iso-8859-1")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("proxy request is not decodable") from exc
        lines = header_text.split("\r\n")
        method, target, _ = _parse_request_line(lines[0])
        headers = _parse_headers(lines[1:])

        if not check_proxy_authorization(headers.get("proxy-authorization"), self.token):
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"Proxy-Authenticate: Bearer realm=samurai-egress\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            denied_host, _, denied_port = target.partition(":")
            self._emit(
                EgressEvent(
                    host=denied_host or target,
                    port=_safe_port(denied_port),
                    verdict=VERDICT_DENIED_AUTH,
                )
            )
            return

        try:
            scheme, host, port, path = parse_request_target(method, target)
        except ToolExecutionError:
            self._emit(EgressEvent(host=target, port=0, verdict=VERDICT_DENIED_PROTOCOL))
            await _respond(writer, 400, b"unsupported proxy request\n")
            return

        verdict = self._verdict(host, port, scheme)
        if verdict != VERDICT_ALLOWED:
            self._emit(EgressEvent(host=host, port=port, verdict=verdict))
            await _respond(writer, 403, b"denied by operation scope\n")
            return

        counters = _Counters()
        try:
            addresses = self.resolver(host)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                addresses[0], port
            )
        except (ToolExecutionError, OSError) as exc:
            self._emit(EgressEvent(host=host, port=port, verdict=VERDICT_DENIED_SCOPE))
            await _respond(writer, 502, f"upstream connect failed: {exc}\n".encode())
            return

        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._tunnel(reader, writer, upstream_reader, upstream_writer, counters)
            else:
                await self._relay_http(
                    method, path, headers, reader, writer,
                    upstream_reader, upstream_writer, counters,
                )
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._emit(
            EgressEvent(
                host=host,
                port=port,
                verdict=VERDICT_ALLOWED,
                bytes_sent=counters.sent,
                bytes_received=counters.received,
            )
        )

    async def _dispatch_socks5(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        method_count = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(method_count) if method_count else b""
        if _SOCKS5_METHOD_USERPASS not in methods:
            writer.write(bytes([_SOCKS5_VERSION, _SOCKS5_METHOD_NONE_ACCEPTABLE]))
            await writer.drain()
            self._emit(EgressEvent(host="", port=0, verdict=VERDICT_DENIED_AUTH))
            return
        writer.write(bytes([_SOCKS5_VERSION, _SOCKS5_METHOD_USERPASS]))
        await writer.drain()

        version = (await reader.readexactly(1))[0]
        if version != _SOCKS5_AUTH_VERSION:
            raise ToolExecutionError("unsupported SOCKS5 auth version")
        username = await _read_socks5_field(reader)
        password = await _read_socks5_field(reader)
        if not self._check_socks5_credentials(username, password):
            writer.write(bytes([_SOCKS5_AUTH_VERSION, 0x01]))
            await writer.drain()
            self._emit(EgressEvent(host="", port=0, verdict=VERDICT_DENIED_AUTH))
            return
        writer.write(bytes([_SOCKS5_AUTH_VERSION, 0x00]))
        await writer.drain()

        version, command, _, address_type = await reader.readexactly(4)
        if version != _SOCKS5_VERSION:
            raise ToolExecutionError("unsupported SOCKS5 request version")
        try:
            host = await _read_socks5_address(reader, address_type)
        except ToolExecutionError:
            await _socks5_reply(writer, _SOCKS5_REP_ATYP_UNSUPPORTED)
            self._emit(
                EgressEvent(host="", port=0, verdict=VERDICT_DENIED_PROTOCOL)
            )
            return
        port = int.from_bytes(await reader.readexactly(2), "big")
        if command != _SOCKS5_CMD_CONNECT:
            await _socks5_reply(writer, _SOCKS5_REP_CMD_UNSUPPORTED)
            self._emit(
                EgressEvent(host=host, port=port, verdict=VERDICT_DENIED_PROTOCOL)
            )
            return

        verdict = self._verdict(host, port, "https")
        if verdict != VERDICT_ALLOWED:
            self._emit(EgressEvent(host=host, port=port, verdict=verdict))
            await _socks5_reply(writer, _SOCKS5_REP_NOT_ALLOWED)
            return

        counters = _Counters()
        try:
            addresses = self.resolver(host)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                addresses[0], port
            )
        except (ToolExecutionError, OSError):
            self._emit(EgressEvent(host=host, port=port, verdict=VERDICT_DENIED_SCOPE))
            await _socks5_reply(writer, _SOCKS5_REP_REFUSED)
            return
        try:
            await _socks5_reply(writer, _SOCKS5_REP_SUCCESS)
            await self._tunnel(reader, writer, upstream_reader, upstream_writer, counters)
        finally:
            upstream_writer.close()
            try:
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        self._emit(
            EgressEvent(
                host=host,
                port=port,
                verdict=VERDICT_ALLOWED,
                bytes_sent=counters.sent,
                bytes_received=counters.received,
            )
        )

    def _check_socks5_credentials(self, username: bytes, password: bytes) -> bool:
        return username.decode("utf-8", "replace") == _SOCKS5_USERNAME and hmac.compare_digest(
            password.decode("utf-8", "replace"), self.token
        )

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        counters: _Counters,
    ) -> None:
        async def pump(
            source: asyncio.StreamReader,
            target: asyncio.StreamWriter,
            count_sent: bool,
        ) -> None:
            while True:
                chunk = await source.read(_READ_CHUNK_SIZE)
                if not chunk:
                    break
                target.write(chunk)
                await target.drain()
                if count_sent:
                    counters.sent += len(chunk)
                else:
                    counters.received += len(chunk)

        forward = asyncio.create_task(pump(client_reader, upstream_writer, True))
        backward = asyncio.create_task(pump(upstream_reader, client_writer, False))
        _, pending = await asyncio.wait(
            {forward, backward}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _relay_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        counters: _Counters,
    ) -> None:
        body = b""
        content_length = headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise ToolExecutionError("invalid content-length") from exc
            if length > _MAX_HEADER_BYTES:
                raise ToolExecutionError("proxy request body too large")
            body = await client_reader.readexactly(length)

        host_header = headers.get("host", "")
        forward = [f"{method} {path} HTTP/1.1"]
        forward.append(f"Host: {host_header}")
        for name, value in headers.items():
            if name in ("host", "proxy-authorization", "proxy-connection", "content-length"):
                continue
            forward.append(f"{name}: {value}")
        if body:
            forward.append(f"Content-Length: {len(body)}")
        forward.append("Connection: close")
        payload = ("\r\n".join(forward) + "\r\n\r\n").encode("iso-8859-1") + body
        upstream_writer.write(payload)
        await upstream_writer.drain()
        counters.sent += len(payload)

        received = 0
        while True:
            chunk = await upstream_reader.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            remaining = self.max_response_bytes - received
            if remaining <= 0:
                break
            client_writer.write(chunk[:remaining])
            await client_writer.drain()
            received += len(chunk[:remaining])
        counters.received += received


_READ_CHUNK_SIZE = 65536


def _parse_request_line(line: str) -> tuple[str, str, str]:
    parts = line.split(" ")
    if len(parts) != 3:
        raise ToolExecutionError(f"invalid proxy request line: {line!r}")
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise ToolExecutionError(f"invalid proxy request version: {line!r}")
    return method.upper(), target, version


def _parse_headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise ToolExecutionError(f"invalid proxy header line: {line!r}")
        headers[name.strip().lower()] = value.strip()
    return headers


async def _read_socks5_field(reader: asyncio.StreamReader) -> bytes:
    length = (await reader.readexactly(1))[0]
    return await reader.readexactly(length) if length else b""


async def _read_socks5_address(
    reader: asyncio.StreamReader, address_type: int
) -> str:
    if address_type == _SOCKS5_ATYP_IPV4:
        return str(ipaddress.IPv4Address(await reader.readexactly(4)))
    if address_type == _SOCKS5_ATYP_IPV6:
        return str(ipaddress.IPv6Address(await reader.readexactly(16)))
    if address_type == _SOCKS5_ATYP_DOMAIN:
        host = (await _read_socks5_field(reader)).decode("ascii", "replace")
        if not host:
            raise ToolExecutionError("empty SOCKS5 domain")
        return host
    raise ToolExecutionError(f"unsupported SOCKS5 address type: {address_type}")


async def _socks5_reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(
        bytes([_SOCKS5_VERSION, code, 0x00, _SOCKS5_ATYP_IPV4])
        + b"\x00\x00\x00\x00"
        + b"\x00\x00"
    )
    await writer.drain()


def _scope_url(host: str, scheme: str) -> str:
    """Scope targets need an IPv6 literal in brackets."""
    literal = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{literal}/"


def _safe_port(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0


async def _respond(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
