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
from samurai_ai.domain.url_scheme import HTTP_SCHEMES

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
        if "$" in literal:
            detail = (
                f"{detail}; assign variables in the same shell call and use "
                ":$PORT for a variable port"
            )
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


def _trim_shell_token(value: str) -> str:
    """Shell separators delimit a command; they are not target characters."""
    return value.strip().strip("'\"").rstrip(";&|)")


_SHELL_PARAMETER = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")


def _strip_url_shell_parameters(value: str) -> str:
    """Remove suffix expansions only when the literal host stays unchanged."""
    if "$" not in value:
        return value
    original = urlsplit(value)
    host = original.hostname
    if not host or "$" in host:
        return value
    cleaned = _SHELL_PARAMETER.sub("", value)
    if urlsplit(cleaned).hostname != host:
        return value
    return cleaned


def _expand_shell_token(value: str, environment: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return environment.get(name, match.group(0))

    return _SHELL_PARAMETER.sub(replace, value)


def _variable_environment(
    tokens: list[str], inherited: dict[str, str] | None
) -> dict[str, str]:
    environment = dict(inherited or {})
    for index, token in enumerate(tokens):
        candidate = tokens[index + 1] if token == "export" and index + 1 < len(tokens) else token
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", candidate)
        if match is None:
            continue
        name, value = match.groups()
        if any(marker in value for marker in ("$(", "`", ";", "|", "&")):
            continue
        environment[name] = value
    for name, value in list(environment.items()):
        environment[name] = _expand_shell_token(value, environment)
    return environment


def _scan_text(
    text: str, environment: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Host-shaped literals in a value that is (a list of) bare targets."""
    found: list[tuple[str, str]] = []
    for part in text.split(","):
        # shlex keeps shell separators attached to the preceding token.
        part = _expand_shell_token(_trim_shell_token(part), environment or {})
        if not part:
            continue
        if "://" in part:
            # a scheme beats the path heuristic: «curl https://host» has no path,
            # so Path().suffix reads «.com» and would have called it a local file
            part = _strip_url_shell_parameters(part)
            parts = urlsplit(part)
            if parts.hostname:
                if parts.scheme.lower() in HTTP_SCHEMES:
                    found.append(("url", part))
                else:
                    # a non-http(s) scheme is not a scope question: judge its
                    # host for scope and leave the scheme to the tool's own
                    # input validation (domain.url_scheme), so a `ssh://` to an
                    # in-scope box is not misreported as "outside the scope"
                    found.append(("host", parts.hostname))
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
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return text.split()


def _literals(
    value: Any, environment: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """Targets in command context, without treating data payloads as destinations."""
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, list | tuple | set):
        out: list[tuple[str, str]] = []
        for item in value:
            out.extend(_literals(item, environment))
        return out
    text = str(value).strip()
    if not text:
        return []
    if " " not in text:
        return _words(text, environment or {})

    from samurai_ai.domain.permission import _executables

    tokens = _tokens(text)
    variables = _variable_environment(tokens, environment)
    executables = _executables(tokens)
    nested: set[int] = set()
    code: set[int] = set()
    data_only: set[int] = set()
    for position, (index, name) in enumerate(executables):
        end = executables[position + 1][0] if position + 1 < len(executables) else len(tokens)
        arguments = tokens[index + 1 : end]
        if name in _DATA_ONLY_COMMANDS:
            data_only.update(range(index + 1, end))
        if name not in _SHELL_COMMANDS | _CODE_COMMANDS:
            continue
        for option in arguments:
            combined_shell = (
                name in _SHELL_COMMANDS and option.startswith("-") and "c" in option
            )
            if option not in ("-c", "-e") and not combined_shell:
                continue
            payload = index + 1 + arguments.index(option) + 1
            if payload < len(tokens):
                (nested if name in _SHELL_COMMANDS else code).add(payload)
            break

    results: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        if index in nested:
            results.extend(_literals(token, variables))
        elif index in code:
            results.extend(_scan_text(token, variables))
        elif index not in data_only:
            results.extend(_words(token, variables))
    return results


_DATA_ONLY_COMMANDS = frozenset({"echo", "printf", "cat", "tee"})
_SHELL_COMMANDS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_CODE_COMMANDS = frozenset({"python", "python3", "perl", "ruby", "node"})


def _words(token: str, environment: dict[str, str]) -> list[tuple[str, str]]:
    """One non-payload token; nested commands are handled by `_literals`."""
    _, _, kv_value = token.partition("=")
    if " " in token:
        return []
    if not kv_value and _looks_like_file(_trim_shell_token(token)):
        return []
    return _scan_text(kv_value or token, environment)


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
