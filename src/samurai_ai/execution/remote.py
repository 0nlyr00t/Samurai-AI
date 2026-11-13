"""Remote administration over a scoped tunnel.

Admins work across Linux, macOS and Windows, and those tools speak their own
protocols (ssh, SMB, RDP, ADB) instead of HTTP. Each call opens a tunnel to the
one target, hands the tool a localhost port, and closes it afterwards — the
verdict is taken once, before the tool starts, and lands in the egress journal.
Credentials come from the vault through `{{secret:name}}` placeholders; ssh keys
are written to the tmpfs first, because ssh only reads keys from files.
"""

from __future__ import annotations

import asyncio
from typing import Any

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolKind,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.native_sandbox import build_bwrap_argv, bwrap_available
from samurai_ai.execution.process import ProcessManager, ProcessResult
from samurai_ai.execution.registry import ToolOutput, stall_as_timeout
from samurai_ai.execution.sandbox import DockerSandboxRunner, get_profile
from samurai_ai.execution.security import _ScopedSandboxTool
from samurai_ai.execution.tunnel import ScopedTunnel

#: grace over the profile timeout for the native (no-docker) fallback
_NATIVE_TIMEOUT_GRACE = 10.0

_HOST_PORT = {
    "host": {"type": "string", "minLength": 1, "maxLength": 253},
    "port": {"type": "integer", "default": 22},
}
_REMOTE_PORTS = frozenset({22, 445, 3389, 5555, 5985, 5986})

_SSH_DRIVER = r"""
import os, subprocess, sys
user, key, command, endpoint = sys.argv[1:5]
path = "/tmp/ssh-key"
with open(path, "w") as handle:
    handle.write(key if key.endswith("\n") else key + "\n")
os.chmod(path, 0o600)
os.environ["TERM"] = "dumb"
argv = [
    "ssh", "-i", path, "-p", endpoint.rsplit(":", 1)[1],
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-T",
    "%s@%s" % (user, endpoint.rsplit(":", 1)[0]), command,
]
raise SystemExit(subprocess.call(argv))
"""


_SSH_PASS_DRIVER = r"""
import os, subprocess, sys
user, command, endpoint = sys.argv[1:4]
host, port = endpoint.rsplit(":", 1)
# the password arrives in the environment (SAMURAI_SSH_PASS), never on argv, so
# it is not visible in the process list. ssh never takes a password on argv or
# stdin; it asks an askpass helper. A tiny helper prints the secret, and
# SSH_ASKPASS_REQUIRE=force (openssh 8.4+) makes ssh use it even without a tty,
# so no sshpass or pty is needed.
askpass = "/tmp/ssh-askpass"
with open(askpass, "w") as handle:
    handle.write('#!/bin/sh\nprintf "%s\\n" "$SAMURAI_SSH_PASS"\n')
os.chmod(askpass, 0o700)
env = dict(os.environ)
env["SSH_ASKPASS"] = askpass
env["SSH_ASKPASS_REQUIRE"] = "force"
env["TERM"] = "dumb"
argv = [
    "ssh", "-p", port,
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "PubkeyAuthentication=no",
    "-o", "PreferredAuthentications=password,keyboard-interactive",
    "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=15", "-T",
    "%s@%s" % (user, host), command,
]
raise SystemExit(subprocess.call(argv, env=env))
"""


class _RemoteTool(_ScopedSandboxTool):
    """A tool that needs a raw TCP connection to one in-scope host."""

    profile_id = "sandbox.remote@v1"
    executable = "python3"
    default_port = 22

    def _host_port(self, params: dict[str, Any]) -> tuple[str, int]:
        host = str(params["host"]).strip()
        port = int(params.get("port") or self.default_port)
        if not 1 <= port <= 65535:
            raise ToolValidationError(f"{self.definition.id}: bad port {port}")
        return host, port

    async def _open(
        self, params: dict[str, Any], scope: ScopeContract
    ) -> ScopedTunnel:
        host, port = self._host_port(params)
        decision = scope.evaluate(host)
        if not decision.allowed:
            raise ToolScopeError(
                f"{self.definition.id}: host {host!r} is out of scope: {decision.detail}"
            )
        # the well-known admin ports plus the exact one the operator named
        tunnel = ScopedTunnel(scope, host, port, allowed_ports=_REMOTE_PORTS | {port})
        await tunnel.start()
        return tunnel

    def preflight(self, params: dict[str, Any]) -> None:
        # the driver is fixed and the endpoint only exists once the tunnel is
        # open, so preflight checks the knobs, the profile and the signature
        self._host_port(params)
        profile = get_profile(self.profile_id)
        self.runner.validate_invocation(profile, self.executable, ["-h"])
        self._notice = self.runner.verify_allowlist_signature()

    def _run(
        self, argv: list[str], params: dict[str, Any], scope: ScopeContract
    ) -> ToolOutput:
        return asyncio.run(self._run_async(argv, params, scope))

    async def _run_async(
        self, argv: list[str], params: dict[str, Any], scope: ScopeContract
    ) -> ToolOutput:
        """Tunnel and process share one loop, so handlers can actually run."""
        tunnel = await self._open(params, scope)
        profile = get_profile(self.profile_id)
        env, secret_env = self.build_env(params)
        native_note = ""
        try:
            if await self.runner.docker_available():
                endpoint = f"host.docker.internal:{tunnel.port}"
                outcome = await self.runner.run_async(
                    profile,
                    "remote",
                    self.executable,
                    [*argv, endpoint],
                    scope,
                    env=env,
                    secret_env=secret_env,
                )
                result: ProcessResult = outcome.result
            else:
                # no Docker: run the driver on the host, confined by bwrap and
                # still reached only through the scoped tunnel (localhost)
                endpoint = f"127.0.0.1:{tunnel.port}"
                result, native_note = await self._run_native(
                    profile, [*argv, endpoint], env, secret_env
                )
            for _ in range(25):
                if tunnel.events:
                    break
                await asyncio.sleep(0.02)
        finally:
            await tunnel.stop()
        events = tunnel.events
        timed_out, note = stall_as_timeout(result)
        stderr = (result.stderr + note).lstrip() if note else result.stderr
        if native_note:
            stderr = f"{native_note}\n{stderr}".strip()
        if events:
            summary = ", ".join(f"{event.host} {event.verdict}" for event in events)
            stderr = (
                f"{stderr}\n[egress: {summary}; "
                f"{tunnel.bytes_sent}+{tunnel.bytes_received}B]"
            ).lstrip()
        return ToolOutput(
            stdout=result.stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=timed_out,
            egress_events=tuple(
                {
                    "host": event.host,
                    "port": event.port,
                    "verdict": event.verdict,
                    "bytes_sent": event.bytes_sent,
                    "bytes_received": event.bytes_received,
                }
                for event in events
            ),
        )

    async def _run_native(
        self,
        profile: Any,
        args: list[str],
        env: dict[str, str],
        secret_env: dict[str, str],
    ) -> tuple[ProcessResult, str]:
        """Run the driver on the host when Docker is unavailable.

        The container is traded for a bubblewrap confinement: a read-only host,
        a writable tmpfs ``/tmp`` for the key, a working ``/dev``, and the shared
        network the tunnel needs. The scope boundary is unchanged — the driver
        still reaches only the one in-scope host through the localhost tunnel.
        When bubblewrap is absent too (e.g. macOS) the driver runs unconfined and
        the note says so.
        """
        inner = [self.executable, *args]
        confined = bwrap_available()
        full = build_bwrap_argv(inner, (), strict=False) if confined else inner
        note = (
            "[native: docker unavailable — ran under bubblewrap, reduced isolation]"
            if confined
            else "[native: docker and bubblewrap unavailable — ran unconfined on the host]"
        )
        manager = ProcessManager(
            allowed_executables=frozenset({"bwrap", self.executable})
        )
        manager.stall_seconds = float(profile.stall_seconds)
        child_env = {**(env or {}), **(secret_env or {})} or None
        extra: dict[str, Any] = {"env": child_env} if child_env else {}
        result = await manager.run(
            full,
            timeout_seconds=profile.timeout_seconds + _NATIVE_TIMEOUT_GRACE,
            **extra,
        )
        return result, note

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        return {"HOME": "/tmp", "TMPDIR": "/tmp"}, {}


class SshExecTool(_RemoteTool):
    """Run a command as an admin over ssh; the private key comes from the vault."""

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.ssh-exec",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Run a shell command on an in-scope host over ssh (admin "
            "work on Linux and macOS); the private key is a vault secret",
            risk_class=RiskClass.R3,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    **_HOST_PORT,
                    "user": {"type": "string", "minLength": 1, "maxLength": 64},
                    "command": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "key": {"type": "string", "minLength": 1},
                },
                "required": ["host", "user", "command", "key"],
                "additionalProperties": False,
            },
            capabilities=("remote", "admin", "linux", "macos"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        return self._run(
            [
                "-c",
                _SSH_DRIVER,
                str(params["user"]),
                str(params["key"]),
                str(params["command"]),
            ],
            params,
            scope,
        )


class SshPassExecTool(_RemoteTool):
    """Run a command as an admin over ssh with a password from the vault.

    The counterpart to :class:`SshExecTool` for hosts that take a password
    rather than a key — the common case for a credential found during an
    operation. ssh is told to use password/keyboard-interactive auth only, and
    the secret reaches it through an askpass helper, never argv or a key file.
    """

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.ssh-pass",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Run a shell command on an in-scope host over ssh using a "
            "password (admin work on Linux and macOS); the password is a vault "
            "secret, e.g. a credential captured during the operation",
            risk_class=RiskClass.R3,
            network=NetworkAccess.PROXIED,
            timeout_seconds=300,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    **_HOST_PORT,
                    "user": {"type": "string", "minLength": 1, "maxLength": 64},
                    "command": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "password": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
                "required": ["host", "user", "command", "password"],
                "additionalProperties": False,
            },
            capabilities=("remote", "admin", "linux", "macos"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        # the password rides in the secret env (redacted, off the process list),
        # not in argv like the key tool's key
        return {"HOME": "/tmp", "TMPDIR": "/tmp"}, {
            "SAMURAI_SSH_PASS": str(params["password"])
        }

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        return self._run(
            [
                "-c",
                _SSH_PASS_DRIVER,
                str(params["user"]),
                str(params["command"]),
            ],
            params,
            scope,
        )


class SmbListTool(_RemoteTool):
    """List SMB shares and the host banner (Windows and Samba)."""

    default_port = 445

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.smb-list",
            version=1,
            kind=ToolKind.SANDBOX,
            description="List SMB shares of an in-scope host (guest/current "
            "credentials); useful before any Windows lateral step",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=180,
            output_limit_bytes=131072,
            parameters={
                "type": "object",
                "properties": {**_HOST_PORT, "port": {"type": "integer", "default": 445}},
                "required": ["host"],
                "additionalProperties": False,
            },
            capabilities=("remote", "smb", "windows"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        host, port = self._host_port(params)
        return self._run(["-c", _SMB_DRIVER, "-L"], {"host": host, "port": port}, scope)


_SMB_DRIVER = r"""
import subprocess, sys
action, endpoint = sys.argv[1], sys.argv[2]
host, port = endpoint.rsplit(":", 1)
argv = [
    "smbclient", "-N", "-g", "-p", port,
    "--option=client min protocol=SMB2", action, "//" + host + "/",
]
raise SystemExit(subprocess.call(argv))
"""


class RdpCheckTool(_RemoteTool):
    """Probe an RDP endpoint without an interactive session."""

    default_port = 3389

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.rdp-check",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Check that an in-scope host answers RDP, which security "
            "mode it asks for and what certificate it presents, without a session",
            risk_class=RiskClass.R2,
            read_only=True,
            network=NetworkAccess.PROXIED,
            timeout_seconds=120,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {**_HOST_PORT, "port": {"type": "integer", "default": 3389}},
                "required": ["host"],
                "additionalProperties": False,
            },
            capabilities=("remote", "rdp", "windows"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_env(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        # the tunnel already is the proxy; keep the driver off the env proxy too
        return {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "NO_PROXY": "host.docker.internal",
            "no_proxy": "host.docker.internal",
        }, {}

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        host, port = self._host_port(params)
        return self._run(["-c", _RDP_DRIVER], {"host": host, "port": port or 3389}, scope)


_RDP_DRIVER = r"""
import socket, ssl, sys

endpoint = sys.argv[1]
host, port = endpoint.rsplit(":", 1)
port = int(port)

# X.224 connection request carrying an RDP negotiation request: TLS, NLA and
# NLA-with-EX. The cookie is what a real client sends; some servers ignore the
# request without one.
cookie = b"Cookie: mstshash=probe\r\n"
# RDP_NEG_REQ: type 1, flags 0, length 8, requested protocols TLS|NLA|NLA-EX
neg = bytes((1, 0)) + (8).to_bytes(2, "little") + (11).to_bytes(4, "little")
x224 = b"\x0e\xe0\x00\x00\x00\x00\x00"
body = x224 + cookie + neg
request = (len(body) + 4).to_bytes(2, "big") + b"\x00\x00" + body

try:
    connection = socket.create_connection((host, port), timeout=10)
except OSError as error:
    print("rdp: no connection: %s" % error)
    raise SystemExit(1)

connection.sendall(request)
try:
    header = connection.recv(4)
except OSError as error:
    print("rdp: no answer to the connection request: %s" % error)
    raise SystemExit(1)
if len(header) < 4:
    print("rdp: short answer, not RDP")
    raise SystemExit(1)
length = int.from_bytes(header[2:4], "big")
answer = b""
while len(answer) < length - 4:
    chunk = connection.recv(length - 4 - len(answer))
    if not chunk:
        break
    answer += chunk
print("rdp: host answered X.224")

# [0:7] x224 confirmation, then the negotiation structure: type, flags, length, value
structure = answer[7:]
kind = structure[0] if structure else 0
# an answer of any kind means the host speaks RDP, so the check is satisfied:
# what it answered belongs in the output, not in the exit code
if kind == 3:  # RDP_NEG_FAILURE
    code = int.from_bytes(structure[4:8], "little")
    print("negotiation: refused by the server, failure code 0x%x" % code)
    raise SystemExit(0)

selected = int.from_bytes(structure[4:8], "little") if kind in (2, 4) else 0
names = {0: "legacy RDP security", 1: "TLS", 2: "HYBRID (NLA)", 8: "HYBRID-EX (NLA)"}
print("negotiation: %s" % names.get(selected, "unknown 0x%x" % selected))

if selected:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        secured = context.wrap_socket(connection, server_hostname=host)
    except OSError as error:
        print("tls: handshake failed: %s" % error)
        raise SystemExit(0)
    print("tls: handshake ok, %s" % secured.version())
    certificate = secured.getpeercert()
    subject = dict(item[0] for item in certificate.get("subject", ()))
    print("certificate: %s, expires %s" % (
        subject.get("commonName") or subject.get("organizationName") or "unnamed",
        certificate.get("notAfter"),
    ))
"""


class AdbTool(_RemoteTool):
    """Reach an Android device or emulator over TCP through the tunnel."""

    default_port = 5555

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.adb",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Talk to an Android device or emulator over ADB: connect, "
            "list devices or run a shell command",
            risk_class=RiskClass.R3,
            network=NetworkAccess.PROXIED,
            timeout_seconds=240,
            output_limit_bytes=131072,
            parameters={
                "type": "object",
                "properties": {
                    **_HOST_PORT,
                    "port": {"type": "integer", "default": 5555},
                    "action": {
                        "type": "string",
                        "enum": ["devices", "shell", "pull"],
                        "default": "devices",
                    },
                    "argument": {"type": "string", "maxLength": 2000},
                },
                "required": ["host"],
                "additionalProperties": False,
            },
            capabilities=("remote", "android", "emulator"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        host, port = self._host_port(params)
        action = str(params.get("action") or "devices")
        argument = str(params.get("argument") or "")
        if action in ("shell", "pull") and not argument:
            raise ToolValidationError(
                f"{self.definition.id}: action {action} needs an argument"
            )
        return self._run(
            ["-c", _ADB_DRIVER, action, argument], {"host": host, "port": port}, scope
        )


_ADB_DRIVER = r"""
import subprocess, sys
endpoint, action, argument = sys.argv[1], sys.argv[2], sys.argv[3]
connect = subprocess.run(
    ["adb", "connect", endpoint], capture_output=True, text=True, timeout=60
)
serial = endpoint
if action == "devices":
    raise SystemExit(subprocess.call(["adb", "devices", "-l"]))
argv = ["adb", "-s", serial, action]
if argument:
    argv += argument.split() if action == "shell" else [argument]
raise SystemExit(subprocess.call(argv))
"""


__all__ = ["AdbTool", "RdpCheckTool", "SmbListTool", "SshExecTool", "SshPassExecTool"]
