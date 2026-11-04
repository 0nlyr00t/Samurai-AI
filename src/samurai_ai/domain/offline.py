"""Air-gapped mode: the harness has no internet, and says so before it tries.

Offline is an operation flag, not a global switch: a lab box may sit behind a VPN
with no route out while models already run on localhost, and the tools that reach
the internet (passive sources, browser research, DoH) would fail halfway if
nothing checked first.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from samurai_ai.domain.execution import ToolDefinition

#: hosts that count as "local" without a resolver
LOCAL_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal", ""}
)
LOCAL_SUFFIXES = (".local", ".localhost", ".internal")


class OfflineError(RuntimeError):
    pass


def is_local_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host in LOCAL_HOSTS:
        return True
    return host.endswith(LOCAL_SUFFIXES)


def needs_internet(tool_id: str) -> bool:
    """Tools that reach the outside by design, whatever their target is."""
    return tool_id.startswith(("research.", "recon.subfinder", "recon.dnsx"))


def check_tool(tool: ToolDefinition, offline: bool) -> None:
    if not offline:
        return
    if needs_internet(tool.id):
        raise OfflineError(
            f"tool {tool.id} needs the internet (passive sources, browser or DoH); "
            "the operation is offline"
        )


def check_model(model_id: str, base_url: str, offline: bool) -> None:
    if not offline:
        return
    if not is_local_url(base_url):
        raise OfflineError(
            f"model {model_id} is remote ({base_url}); an offline operation only "
            "accepts local endpoints such as ollama on 127.0.0.1"
        )


__all__ = [
    "LOCAL_HOSTS",
    "LOCAL_SUFFIXES",
    "OfflineError",
    "check_model",
    "check_tool",
    "is_local_url",
    "needs_internet",
]
