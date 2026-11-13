"""Scheme validation for tools that can only reach http(s) targets.

A non-http(s) URL reaching an http-only tool (``recon.httpx-probe``,
``research.read``, ``fuzz.ffuf``, the url-host manifests, …) is a *misrouted*
call, not an out-of-scope target: the host is very often perfectly in scope.
Reporting it through the scope decision ("only http and https targets are
supported", wrapped as "outside the operation scope") sent the model in
circles — it read the message as a scope problem and retried the same
``ssh://`` URL over and over.

The scheme is therefore treated as *input validation*, deliberately separate
from the scope decision: this module turns a non-http(s) scheme into a clear
``ToolValidationError`` that names the scheme and points at the governed tool
that actually speaks it.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from samurai_ai.domain.execution import ToolValidationError

#: the only schemes an http-only tool can reach
HTTP_SCHEMES = frozenset({"http", "https"})

#: scheme -> the governed tool that speaks it, so the message can route the
#: model instead of leaving it to guess. Unlisted schemes fall back to the
#: generic "http(s) only" hint.
_SCHEME_TOOL: dict[str, str] = {
    "ssh": "remote.ssh-pass",
    "sftp": "remote.ssh-pass",
    "scp": "remote.ssh-pass",
    "rdp": "remote.rdp-check",
    "ms-wbt-server": "remote.rdp-check",
    "smb": "remote.smb-list",
    "cifs": "remote.smb-list",
    "adb": "remote.adb",
}


def named_scheme(value: str) -> str:
    """The URL scheme of ``value``, but only when it is a real ``scheme://host``.

    A bare host or ``host:port`` names no scheme here: ``urlsplit`` reads the
    host of ``example.com:8443`` as the *scheme* ``example.com``, which would be
    a false positive. Requiring a parsed hostname (which only a ``://`` netloc
    produces) keeps ``example.com:8443`` and ``10.0.0.5`` free of a scheme.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    return parts.scheme.lower()


def scheme_hint(scheme: str) -> str:
    """How to tell the model where a non-http(s) scheme belongs."""
    tool = _SCHEME_TOOL.get(scheme)
    if tool is not None:
        return f"use {tool} for {scheme} targets"
    return "this tool only accepts http:// and https:// targets"


def require_http_scheme(tool_id: str, parameter: str, value: str) -> None:
    """Reject ``value`` when it names a scheme other than http(s).

    A bare host, an IP, or a ``host:port`` names no scheme and is left alone —
    only an explicit non-http(s) URL scheme is the misrouting this guards. This
    is input validation, not a scope decision: the host may well be in scope.
    """
    scheme = named_scheme(value)
    if scheme and scheme not in HTTP_SCHEMES:
        raise ToolValidationError(
            f"{tool_id}: parameter {parameter!r} uses the {scheme!r} scheme, which "
            f"this http-only tool cannot reach: {value!r} — {scheme_hint(scheme)}"
        )


__all__ = [
    "HTTP_SCHEMES",
    "named_scheme",
    "require_http_scheme",
    "scheme_hint",
]
