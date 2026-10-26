"""HTTP egress broker for managed tool execution.

Stage 2b scope control without DNS TOCTOU: every request URL is evaluated
against the operation scope, the hostname is resolved exactly once, every
resolved address is checked against the scope exclusions, and the connection
is then made to one of those pinned addresses — the HTTP Host header (and,
for HTTPS, the TLS SNI/server_hostname used for certificate validation) still
carries the original hostname, so a resolver that changes its answer after
the check cannot redirect the connection elsewhere. Redirect targets go
through the same pipeline (fail-closed, at most 3 redirects).

GET, HEAD and POST (with an explicit body, used by the MCP transport) are
supported and credentials in URLs are rejected; the http.fetch tool itself
only exposes GET/HEAD through its parameter schema.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message

from samurai_ai.domain.execution import ToolExecutionError, ToolScopeError, ToolValidationError
from samurai_ai.domain.scope import ScopeContract, ScopeDecisionReason

MAX_REDIRECTS = 3
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_READ_CHUNK = 65536

Resolver = Callable[[str], list[str]]


def default_resolver(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ToolExecutionError(f"DNS resolution failed for {hostname}: {exc}") from exc
    addresses = sorted({str(info[4][0]) for info in infos})
    if not addresses:
        raise ToolExecutionError(f"DNS resolution returned no addresses for {hostname}")
    return addresses


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    reason: str
    body: bytes
    truncated: bool
    redirects: tuple[str, ...]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self, host: str, port: int, addresses: list[str], timeout: float
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_addresses = addresses
        self._pinned_timeout = timeout

    def connect(self) -> None:
        self.sock = _dial_pinned(self._pinned_addresses, self.port, self._pinned_timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, host: str, port: int, addresses: list[str], timeout: float,
        verify_tls: bool = True,
    ) -> None:
        self._ssl_context = ssl.create_default_context()
        if not verify_tls:
            # Explicit operator opt-out per endpoint (e.g. networks that
            # require a national MITM root certificate). Hostname checking
            # and certificate validation are both disabled for this endpoint.
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port=port, timeout=timeout, context=self._ssl_context)
        self._pinned_addresses = addresses
        self._pinned_timeout = timeout

    def connect(self) -> None:
        raw = _dial_pinned(self._pinned_addresses, self.port, self._pinned_timeout)
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)


def _dial_pinned(addresses: list[str], port: int, timeout: float) -> socket.socket:
    last_error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=timeout)
        except OSError as exc:
            last_error = exc
    raise ToolExecutionError(
        f"connect failed for all pinned addresses {addresses}: {last_error}"
    )


class HttpEgressBroker:
    def __init__(
        self,
        scope: ScopeContract,
        resolver: Resolver = default_resolver,
        verify_tls: bool = True,
    ) -> None:
        self.scope = scope
        self.resolver = resolver
        self.verify_tls = verify_tls

    def resolve_and_check(self, hostname: str) -> list[str]:
        addresses = self.resolver(hostname)
        if not addresses:
            raise ToolExecutionError(f"DNS resolution returned no addresses for {hostname}")
        for address in sorted(addresses):
            decision = self.scope.evaluate(address)
            if decision.reason == ScopeDecisionReason.EXCLUDED:
                raise ToolScopeError(
                    f"hostname {hostname} resolves to explicitly excluded address {address}"
                )
        return sorted(addresses)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
        max_bytes: int = 65536,
        max_redirects: int = MAX_REDIRECTS,
    ) -> HttpResponse:
        verb = method.upper()
        if verb not in _ALLOWED_METHODS:
            raise ToolValidationError(
                f"http method {method!r} is not allowed (GET/HEAD/POST only)"
            )
        if body is not None and verb != "POST":
            raise ToolValidationError(f"request body is only allowed with POST, not {verb}")
        if verb == "POST" and body is None:
            raise ToolValidationError("POST requests require a body")
        redirects: list[str] = []
        current = url
        current_verb = verb
        current_body = body
        while True:
            self._validate_url(current)
            response = self._pinned_request(
                current, current_verb, current_body, headers, timeout, max_bytes
            )
            if response.status in (301, 302, 303, 307, 308):
                if len(redirects) >= max_redirects:
                    raise ToolExecutionError(
                        f"redirect limit exceeded ({max_redirects}) for {url}"
                    )
                location = response.headers.get("Location")
                if not location:
                    raise ToolExecutionError(f"redirect without Location header from {current}")
                current = urllib.parse.urljoin(current, location)
                redirects.append(current)
                if response.status == 303:
                    current_verb = "GET"
                    current_body = None
                continue
            return HttpResponse(
                url=current,
                status=response.status,
                reason=response.reason,
                body=response.body,
                truncated=response.truncated,
                redirects=tuple(redirects),
            )

    def _validate_url(self, url: str) -> None:
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise ToolValidationError(f"invalid URL: {url!r}") from exc
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise ToolValidationError(f"URL scheme must be http or https: {url!r}")
        if parts.username is not None or parts.password is not None:
            raise ToolValidationError("credentials in URLs are not allowed")
        hostname = parts.hostname
        if not hostname:
            raise ToolValidationError(f"URL must contain a hostname: {url!r}")

        decision = self.scope.evaluate(url)
        if not decision.allowed:
            raise ToolScopeError(
                f"URL {url} is not in operation scope: "
                f"{decision.reason.value} ({decision.detail})"
            )

    def _pinned_request(
        self,
        url: str,
        method: str,
        body: bytes | None,
        headers: dict[str, str] | None,
        timeout: float,
        max_bytes: int,
    ) -> _RawResponse:
        parts = urllib.parse.urlsplit(url)
        hostname = parts.hostname or ""
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        addresses = self.resolve_and_check(hostname)
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection: http.client.HTTPConnection
        if parts.scheme.lower() == "https":
            connection = _PinnedHTTPSConnection(
                hostname, port, addresses, timeout, verify_tls=self.verify_tls
            )
        else:
            connection = _PinnedHTTPConnection(hostname, port, addresses, timeout)
        host_header = hostname if parts.port is None else f"{hostname}:{parts.port}"
        request_headers = {
            "Host": host_header,
            "User-Agent": "samurai-ai/0.1 (managed-tool-execution)",
            "Accept": "*/*",
            "Connection": "close",
        }
        for name, value in (headers or {}).items():
            request_headers[name] = value
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload, truncated = self._read_bounded(response, max_bytes)
            return _RawResponse(
                status=response.status,
                reason=response.reason or "",
                headers=response.headers,
                body=payload,
                truncated=truncated,
            )
        except (OSError, http.client.HTTPException) as exc:
            raise ToolExecutionError(f"http request failed for {url}: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _read_bounded(stream: http.client.HTTPResponse, max_bytes: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        captured = 0
        truncated = False
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                break
            remaining = max_bytes + 1 - captured
            if remaining > 0:
                chunks.append(chunk[:remaining])
                captured += len(chunk[:remaining])
            if captured > max_bytes:
                truncated = True
                break
        body = b"".join(chunks)[:max_bytes]
        return body, truncated


@dataclass(frozen=True)
class _RawResponse:
    status: int
    reason: str
    headers: Message
    body: bytes
    truncated: bool
