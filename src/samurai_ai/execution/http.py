"""HTTP egress broker for managed tool execution.

Stage 2a scope control: every request URL is evaluated against the operation
scope, the hostname is resolved via DNS before connecting and explicitly
excluded addresses are refused, and each redirect target is re-validated
through the same pipeline (fail-closed, at most 3 redirects). Only GET and
HEAD are supported and credentials in URLs are rejected.

This is NOT a complete egress enforcement layer: the DNS check and the
actual connection are separate lookups (TOCTOU), so a hostile resolver can
still return a different address at connect time. Closing that gap requires
connecting to the validated address directly, which is Stage 2b work.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any

from samurai_ai.domain.execution import ToolExecutionError, ToolScopeError, ToolValidationError
from samurai_ai.domain.scope import ScopeContract, ScopeDecisionReason

MAX_REDIRECTS = 3
_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_READ_CHUNK = 65536


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    reason: str
    body: bytes
    truncated: bool
    redirects: tuple[str, ...]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


class HttpEgressBroker:
    def __init__(self, scope: ScopeContract) -> None:
        self.scope = scope
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        timeout: float = 10.0,
        max_bytes: int = 65536,
        max_redirects: int = MAX_REDIRECTS,
    ) -> HttpResponse:
        verb = method.upper()
        if verb not in _ALLOWED_METHODS:
            raise ToolValidationError(f"http method {method!r} is not allowed (GET/HEAD only)")
        redirects: list[str] = []
        current = url
        while True:
            self._validate_url(current)
            response = self._single_request(current, verb, timeout, max_bytes)
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
        self._check_resolved_addresses(hostname)

    def _check_resolved_addresses(self, hostname: str) -> None:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise ToolExecutionError(f"DNS resolution failed for {hostname}: {exc}") from exc
        addresses = {str(info[4][0]) for info in infos}
        if not addresses:
            raise ToolExecutionError(f"DNS resolution returned no addresses for {hostname}")
        for address in sorted(addresses):
            decision = self.scope.evaluate(address)
            if decision.reason == ScopeDecisionReason.EXCLUDED:
                raise ToolScopeError(
                    f"hostname {hostname} resolves to explicitly excluded address {address}"
                )

    def _single_request(
        self, url: str, method: str, timeout: float, max_bytes: int
    ) -> _RawResponse:
        request = urllib.request.Request(url, method=method)
        request.add_header("User-Agent", "samurai-ai/0.1 (managed-tool-execution)")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body, truncated = self._read_bounded(response, max_bytes)
                return _RawResponse(
                    status=response.status,
                    reason=response.reason or "",
                    headers=response.headers,
                    body=body,
                    truncated=truncated,
                )
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                return _RawResponse(
                    status=exc.code,
                    reason=exc.reason or "",
                    headers=exc.headers,
                    body=b"",
                    truncated=False,
                )
            body, truncated = self._read_bounded(exc, max_bytes)
            return _RawResponse(
                status=exc.code,
                reason=exc.reason or "",
                headers=exc.headers,
                body=body,
                truncated=truncated,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolExecutionError(f"http request failed for {url}: {exc}") from exc

    @staticmethod
    def _read_bounded(stream: Any, max_bytes: int) -> tuple[bytes, bool]:
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
