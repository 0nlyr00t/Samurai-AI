"""One scope check for every tool call, at the common execution path.

Adapters already check their own targets, and that is the first line. This is the
second: it scans every parameter value for host-shaped literals and refuses anything
outside the operation scope, so an adapter that forgets its check (or a new one written
in a hurry) cannot silently reach out. A parameter an adapter knows is not a target
(a resolver URL, a source list) opts out by name.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from samurai_ai.domain.execution import ToolScopeError
from samurai_ai.domain.scope import ScopeContract, ScopeDecision

#: a bare token that looks like a domain and nothing else
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z", re.I)
_HOST_PORT = re.compile(r"([a-z0-9.-]+|\[[0-9a-f:]+\]):\d{1,5}\Z", re.I)


class ScopeGuardError(ToolScopeError):
    """Same family as an adapter's own refusal, so callers already handle it."""


def _literals(value: Any) -> list[tuple[str, str]]:
    """(kind, literal) pairs: a URL is judged as a URL so scheme rules still match."""
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, list | tuple | set):
        out: list[tuple[str, str]] = []
        for item in value:
            out.extend(_literals(item))
        return out
    text = str(value).strip()
    if not text:
        return []
    found: list[tuple[str, str]] = []
    for part in text.split(","):
        part = part.strip()
        if not part or _is_local_path(part):
            continue
        if "://" in part:
            if urlsplit(part).hostname:
                found.append(("url", part))
            continue
        if ipaddress_ok(part):
            found.append(("net" if "/" in part else "host", part))
            continue
        host_port = _HOST_PORT.fullmatch(part)
        if host_port:
            found.append(("host", host_port.group(1).strip("[]")))
            continue
        if _DOMAIN.fullmatch(part):
            found.append(("host", part))
    return found


def _is_local_path(value: str) -> bool:
    """A path that exists on this machine is not a host, whatever it looks like."""
    if value.startswith(("/", "./", "~")):
        return True
    return "/" in value and "." in value.rsplit("/", 1)[-1] and Path(value).suffix != ""


def ipaddress_ok(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def _decide(scope: ScopeContract, kind: str, literal: str) -> ScopeDecision:
    """A URL first goes through URL rules; a host may also be covered by a network."""
    decision = scope.evaluate(literal)
    if decision.allowed:
        return decision
    if kind == "host":
        by_network = scope.evaluate_network(f"{literal}/32")
        if by_network.allowed:
            return by_network
    elif kind == "net":
        return scope.evaluate_network(literal)
    return decision


def guard(
    scope: ScopeContract, params: dict[str, Any], *, exempt: tuple[str, ...] = ()
) -> None:
    """Refuse the first literal that is outside the scope; nothing is checked in exempt."""
    for name, value in params.items():
        if name in exempt:
            continue
        for kind, literal in _literals(value):
            decision = _decide(scope, kind, literal)
            if not decision.allowed:
                raise ScopeGuardError(
                    f"{literal!r} (parameter {name!r}) is outside the operation scope: "
                    f"{decision.detail}"
                )


__all__ = ["ScopeGuardError", "guard", "ipaddress_ok"]
