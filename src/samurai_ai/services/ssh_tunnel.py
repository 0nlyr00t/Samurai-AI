"""Reach a daemon on another machine over SSH.

The daemon already speaks a socket protocol, so "distributed" starts as a tunnel:
forward the remote socket to a private local one and let every existing client
talk to it unchanged. Nothing new crosses the wire, and the only trust added is
the SSH host key.

A unix socket is forwarded, not a TCP port: the remote side keeps its 0600 file,
so an ssh login without access to the state directory still cannot connect.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REMOTE_SOCKET = "$HOME/.config/samurai/run/serve.sock"
WAIT_SECONDS = 15.0


class TunnelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Destination:
    host: str
    user: str = ""
    port: int = 0

    @classmethod
    def parse(cls, value: str) -> Destination:
        """`[user@]host[:port]`, the shape people already type for ssh."""
        target = value.strip()
        if not target:
            raise TunnelError("empty destination")
        user, _, host_port = target.rpartition("@")
        host, _, port_text = host_port.partition(":")
        if not host:
            raise TunnelError(f"cannot read a host out of {value!r}")
        port = int(port_text) if port_text.isdigit() else 0
        return cls(host=host, user=user, port=port)

    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


class SshTunnel:
    def __init__(
        self,
        destination: Destination,
        local_socket: Path,
        *,
        remote_socket: str = DEFAULT_REMOTE_SOCKET,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self.destination = destination
        self.local_socket = local_socket
        self.remote_socket = remote_socket
        self.extra_args = extra_args
        self.process: subprocess.Popen[bytes] | None = None

    def argv(self) -> list[str]:
        """The exact ssh call; separate so a test can read it without a network."""
        args = [
            "ssh",
            "-N",  # no remote command: the forward is the whole job
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-L", f"{self.local_socket}:{self.remote_socket}",
        ]
        if self.destination.port:
            args += ["-p", str(self.destination.port)]
        args += list(self.extra_args)
        args.append(self.destination.ssh_target())
        return args

    @property
    def remote_expanded(self) -> str:
        """The remote path as a shell would read it (tilde included)."""
        return self.remote_socket

    def open(self, wait_seconds: float = WAIT_SECONDS) -> Path:
        if self.local_socket.exists():
            self.local_socket.unlink()
        self.process = subprocess.Popen(  # noqa: S603 - argv built here
            self.argv(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.local_socket.exists():
                return self.local_socket
            if self.process.poll() is not None:
                stream = self.process.stderr
                detail = (stream.read() if stream is not None else b"").decode().strip()
                raise TunnelError(
                    "ssh exited before the forward came up"
                    + (f": {detail.splitlines()[-1]}" if detail else "")
                )
            time.sleep(0.1)
        self.close()
        raise TunnelError(f"the forward did not appear within {wait_seconds:.0f}s")

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn ssh
                self.process.kill()
        self.process = None
        self.local_socket.unlink(missing_ok=True)


def describe(destination: Destination, remote_socket: str = DEFAULT_REMOTE_SOCKET) -> str:
    """One line for the operator: what will be reached, and how."""
    return (
        f"ssh {shlex.quote(destination.ssh_target())} "
        f"forwarding {shlex.quote(remote_socket)}"
    )


__all__ = [
    "DEFAULT_REMOTE_SOCKET",
    "Destination",
    "SshTunnel",
    "TunnelError",
    "describe",
]
