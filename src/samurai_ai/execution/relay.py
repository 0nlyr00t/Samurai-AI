"""Run a network tool through a relay prefix instead of a container.

A VPN often lives one hop away — in a Parallels/UTM guest, behind an SSH jump
host. The host has no route to the target, so every container the sandbox spawns
is blind, and the only working path used to be a raw shell. The relay prefix
(``prlctl exec '<uuid>'``, ``ssh -J jump``) puts the tool on the far side of that
hop while the harness keeps the scope guard, the artifact trail and the parser.

The container is skipped entirely: ``bash -lc "<relay> <tool> <args>"`` runs on
the host. Fixed argv survives the trip quoted, so a model-supplied value cannot
break out of the command.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import Any

from samurai_ai.domain.execution import ToolExecutionError, ToolValidationError
from samurai_ai.domain.sandbox import SandboxProfile
from samurai_ai.execution.egress import DEFAULT_ALLOWED_PORTS
from samurai_ai.execution.sandbox import DockerSandboxRunner

#: what a relay prefix may look like: no NUL, no leading dash (it is not argv)
MAX_RELAY_LENGTH = 400


def validate_relay(relay: str) -> str:
    """Reject a prefix that would not survive `bash -lc`."""
    value = relay.strip()
    if not value:
        raise ToolValidationError("relay prefix is empty")
    if len(value) > MAX_RELAY_LENGTH:
        raise ToolValidationError(f"relay prefix is longer than {MAX_RELAY_LENGTH}")
    if "\x00" in value:
        raise ToolValidationError("relay prefix cannot contain NUL bytes")
    return value


def relay_command(relay: str, executable: str, args: list[str]) -> str:
    """The single shell line a relayed call becomes."""
    return f"{relay} {shlex.join([executable, *args])}"


class RelaySandboxRunner(DockerSandboxRunner):
    """Docker runner that hands the command to a relay prefix instead."""

    def __init__(
        self,
        manager: Any,
        signature_path: Any,
        master_key_loader: Any,
        relay: str,
        egress_allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
    ) -> None:
        super().__init__(
            manager,
            signature_path,
            master_key_loader,
            egress_allowed_ports=egress_allowed_ports,
        )
        self.relay = validate_relay(relay)

    def build_argv(
        self,
        profile: SandboxProfile,
        run_id: str,
        executable: str,
        args: list[str],
        proxy_url: str | None = None,
        env: dict[str, str] | None = None,
        secret_env: dict[str, str] | None = None,
        hosts: dict[str, str] | None = None,
        network: str | None = None,
    ) -> list[str]:
        del run_id, proxy_url, hosts, network
        # the same allowlist and NUL checks as the container path
        self.validate_invocation(profile, executable, args)
        if secret_env:
            self.validate_secret_env(profile, secret_env)
        return ["bash", "-lc", relay_command(self.relay, executable, args)]

    async def preflight(self, profile: SandboxProfile) -> str | None:
        """No image to look for: the far side owns its own tooling."""
        return self.verify_allowlist_signature()


@dataclass(frozen=True)
class RelayResponse:
    """What a relayed fetch answered."""

    status: int
    headers: str
    body: bytes


def relayed_fetch(
    relay: str,
    url: str,
    *,
    method: str,
    timeout: float,
    max_bytes: int,
    manager: Any,
) -> RelayResponse:
    """Fetch a URL with curl on the far side of the relay.

    The host has no route, so its own http.client cannot connect; curl runs
    where the route exists and only travel-safe limits are applied here.
    """
    argv = [
        "bash",
        "-lc",
        relay_command(
            relay,
            "curl",
            [
                "-sS",
                "-i",
                "--max-time",
                str(max(1, int(timeout))),
                "--max-filesize",
                str(max_bytes),
                "-X",
                method,
                url,
            ],
        ),
    ]
    result = asyncio.run(manager.run(argv, timeout_seconds=timeout + 5))
    if result.exit_code not in (0, None) or result.timed_out:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise ToolExecutionError(f"relayed fetch failed for {url}: {detail}")
    raw = result.stdout.encode("utf-8", errors="surrogateescape")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").splitlines()
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers = "\r\n".join(lines[1:])
    return RelayResponse(status=status, headers=headers, body=body[:max_bytes])


def build_runner(relay: str) -> RelaySandboxRunner:
    """A relay runner wired to a host shell, ready to hand to an adapter."""
    from samurai_ai.config import sandbox_allowlist_path, vault_key_path
    from samurai_ai.execution.process import ProcessManager
    from samurai_ai.services.vault import load_master_key

    return RelaySandboxRunner(
        ProcessManager(allowed_executables=frozenset({"bash"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
        relay,
    )


__all__ = [
    "MAX_RELAY_LENGTH",
    "RelaySandboxRunner",
    "build_runner",
    "relay_command",
    "relayed_fetch",
    "RelayResponse",
    "validate_relay",
]
