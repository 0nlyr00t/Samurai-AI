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

#: real top-level domains. The loose "letters after a dot" rule called
#: `urllib.request` and `import os` hosts and refused them; a suffix nobody
#: registers is either a module path or a file, not a target.
_TLDS = frozenset(
    """
    com net org edu gov mil int io co dev app cloud tech info biz xyz online site
    store blog shop ai me tv cc ws gg to sh js md py ru ua by kz de fr uk nl it es
    pl se no fi dk cz at ch be pt gr ro bg hu ie il in jp cn kr sg hk tw au nz ca
    us mx br ar cl pe ve za ng ke eg ma tr ae sa ir pk bd lk th vn id my ph
    htb htb-cloud lab labs lan local internal corp home test example invalid
    """.split()
)
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63})\Z", re.I)


def _known_tld(value: str) -> bool:
    match = _DOMAIN.fullmatch(value)
    return match is not None and match.group(1).lower() in _TLDS
_HOST_PORT = re.compile(r"([a-z0-9.-]+|\[[0-9a-f:]+\]):\d{1,5}\Z", re.I)
#: curl --resolve=host:port:ip — the IP is where the traffic actually goes
#: extensions that read as a file in a command line even though the TLD is real:
#: `cat app.js` was judged a domain and refused («.js» is a TLD, so it matched)
_FILE_EXTENSIONS = frozenset(
    """
    js jsx ts tsx mjs cjs json py rb go rs php html htm css scss txt md rst yml yaml
    toml ini conf config sh bash zsh ps1 bat cmd exe dll bin env log csv tsv xml sql
    db sqlite bak zip tar gz tgz bz2 xz 7z pem crt key pub lock mod sum
    """.split()
)

_HOST_PORT_IP = re.compile(
    r"[a-z0-9.-]+:\d{1,5}:(\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:]+\])\Z", re.I
)


class ScopeGuardError(ToolScopeError):
    """Same family as an adapter's own refusal, so callers already handle it.

    The offending resource is kept as a field, not just inside the message: the
    harness offers it to the operator instead of making them retype it.
    """

    def __init__(self, literal: str, parameter: str, detail: str) -> None:
        self.literal = literal
        self.parameter = parameter
        self.detail = detail
        super().__init__(
            f"{literal!r} (parameter {parameter!r}) is outside the operation scope: {detail}"
        )


#: Hosts that are plumbing, not targets. A run that resolves a name or asks the
#: platform API where the box is does not widen the contract, and refusing it cost
#: a whole model round trip each time. Edited per platform suffix, so a new
#: subdomain of the same control plane needs no code change.
_SHARED_INFRA: frozenset[str] = frozenset(
    {
        "1.1.1.1",
        "1.0.0.1",
        "8.8.8.8",
        "8.8.4.4",
        "9.9.9.9",
        "149.112.112.112",
        "208.67.222.222",
        "208.67.220.220",
        "github.com",
        "raw.githubusercontent.com",
        "api.github.com",
    }
)
_PLATFORM_INFRA: dict[str, frozenset[str]] = {
    "ctf-htb": frozenset({".hackthebox.com", ".hackthebox.eu"}),
    "bug-bounty": frozenset(
        {".hackerone.com", ".bugcrowd.com", ".intigriti.com", ".yeswehack.com"}
    ),
}


def infra_hosts(mode: str) -> frozenset[str]:
    """The always-allowed plumbing for an operation mode."""
    return _SHARED_INFRA | _PLATFORM_INFRA.get(mode, frozenset())


def _scan_text(text: str) -> list[tuple[str, str]]:
    """Host-shaped literals in a value that is (a list of) bare targets."""
    found: list[tuple[str, str]] = []
    for part in text.split(","):
        part = part.strip().strip("'\"")
        if not part:
            continue
        if "://" in part:
            # a scheme beats the path heuristic: «curl https://host» has no path,
            # so Path().suffix reads «.com» and would have called it a local file
            if urlsplit(part).hostname:
                found.append(("url", part))
            continue
        if _is_local_path(part):
            continue
        if ipaddress_ok(part):
            # the unspecified address (`::`, `0.0.0.0`) is a bind-any, not a
            # destination — a bare `::` in a command is syntax, never a target
            if ipaddress.ip_network(part, strict=False).network_address.is_unspecified:
                continue
            found.append(("net" if "/" in part else "host", part))
            continue
        host_port = _HOST_PORT.fullmatch(part)
        if host_port:
            found.append(("host", host_port.group(1).strip("[]")))
            continue
        if _known_tld(part):
            found.append(("host", part))
            continue
        resolve = _HOST_PORT_IP.fullmatch(part)
        if resolve:
            found.append(("host", resolve.group(1).strip("[]")))
    return found


def _looks_like_file(token: str) -> bool:
    """A bare word with a code/file suffix: `app.js`, `c.txt`, `hosts.bak`."""
    if "/" in token:
        return False
    _, _, suffix = token.rpartition(".")
    return bool(suffix) and suffix.lower() in _FILE_EXTENSIONS


def _tokens(text: str) -> list[str]:
    """A free-form command line split into tokens; a broken quote splits plain."""
    import shlex

    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _literals(value: Any) -> list[tuple[str, str]]:
    """(kind, literal) pairs: a URL is judged as a URL so scheme rules still match.

    A free-form command (shell.exec) is not a bare host, so it is judged per
    token — `nmap -Pn 10.0.0.5` must not slip through whole-string matching.
    """
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
    if " " not in text:
        # a lone word may be a file as much as a host: `send.sh` is a script, and
        # «.sh» is a registered TLD, so the file check has to come first
        return _words(text)
    found = _scan_text(text)
    if found:
        return found
    out = []
    for token in _tokens(text):
        out.extend(_words(token))
    return out


def _words(token: str) -> list[tuple[str, str]]:
    """What one token contributes; a file name contributes nothing.

    A quoted tail is a command of its own («bash -c 'nmap 10.0.0.5'»), so it is
    split again rather than judged as one dotted word.
    """
    _, _, kv_value = token.partition("=")
    if " " not in token:
        if not kv_value and _looks_like_file(token):
            # a file, not a host: `cat app.js` must not be judged as the .js TLD
            return []
        return _scan_text(kv_value or token)
    found: list[tuple[str, str]] = []
    for inner in _tokens(token):
        found.extend(_words(inner))
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
    """Refuse the first literal that is outside the scope; nothing is checked in exempt.

    Infra hosts need no special case here: they ride on the contract (`infra`), so
    `evaluate` already allows them.
    """
    for name, value in params.items():
        if name in exempt:
            continue
        for kind, literal in _literals(value):
            decision = _decide(scope, kind, literal)
            if not decision.allowed:
                raise ScopeGuardError(literal, name, decision.detail)


__all__ = ["ScopeGuardError", "guard", "infra_hosts", "ipaddress_ok"]
