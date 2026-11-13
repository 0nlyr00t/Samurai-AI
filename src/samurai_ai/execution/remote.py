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


# Helpers shared by the two RDP drivers. They are kept in one string so the
# code the sandbox runs is the exact code the unit tests exercise: a test
# ``exec``s this block (with os/re imported) and calls the functions directly,
# which is why the real exit-code/log contract below is covered without a live
# server. Drivers run as ``python3 -c`` inside the belt, so they cannot import
# ``samurai_ai`` — the logic has to travel as source, not as an import.
_RDP_SHARED = r'''
def _rdp_recv_exact(connection, size):
    """Read exactly ``size`` bytes; return fewer only on EOF or timeout.

    TCP is a stream: a single ``recv(4)`` can return one, two or three bytes of
    a four-byte TPKT header even though the rest is on the way. The old probe
    read the header with a bare ``recv(4)`` and, whenever the header arrived in
    pieces, declared "short answer, not RDP" for a host that had in fact
    answered — the same probe sequence flapping between the two verdicts. This
    loops until the bytes are in or the peer goes away.
    """
    buffer = b""
    while len(buffer) < size:
        try:
            chunk = connection.recv(size - len(buffer))
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
    return buffer


def rdp_parse_negotiation(response):
    """Describe the RDP negotiation answer; return (lines, selected_protocol).

    ``response`` is the X.224 payload after the 4-byte TPKT header. A selected
    protocol > 0 means the caller should start TLS; 0 means standard RDP
    security (no TLS); -1 means the server refused and nothing more should be
    attempted.
    """
    structure = response[7:]
    if not structure:
        return (["negotiation: no response, standard RDP security"], 0)
    kind = structure[0]
    if kind == 3:  # TYPE_RDP_NEG_FAILURE
        code = int.from_bytes(structure[4:8], "little")
        return (["negotiation: refused by the server, failure code 0x%x" % code], -1)
    if kind != 2:  # expected TYPE_RDP_NEG_RSP
        return (["negotiation: unexpected response type %d" % kind], 0)
    selected = int.from_bytes(structure[4:8], "little")
    names = {0: "standard RDP security", 1: "TLS", 2: "HYBRID (NLA)", 8: "HYBRID-EX (NLA)"}
    return (["negotiation: %s" % names.get(selected, "unknown 0x%x" % selected)], selected)


def rdp_auth_verdict(exit_code, log):
    """Classify an xfreerdp ``/auth-only`` run from its exit code and log.

    The process exit code alone is not trustworthy. FreeRDP 3 prints the ERROR
    line ``Authentication only, exit status N`` on both success and failure, and
    a run that never reached the logon (``ERRCONNECT_CONNECT_CANCELLED``,
    a cancelled/dropped CredSSP negotiation) is not a statement about the
    credentials at all. Reading exit-status text or a bare rc=0 is exactly how
    the field wrapper reported AUTH_OK while xfreerdp had actually cancelled.

    The authoritative signal is the ``freerdp_get_last_error`` blob,
    ``ERRCONNECT_<NAME> [0x000200XX]``. A credential-class code means the server
    answered and rejected the login (AUTH_FAIL); any other non-zero code means
    the login could not be tested (AUTH_ERROR, never reported as valid creds);
    only exit 0 with no non-zero error blob is AUTH_OK. The error blob wins over
    the exit code, so a build that prints CANCELLED yet exits 0 is still ERROR.
    """
    names = {
        0x00020006: "CONNECT_FAILED",
        0x00020008: "TLS_CONNECT_FAILED",
        0x00020009: "AUTHENTICATION_FAILED",
        0x0002000A: "INSUFFICIENT_PRIVILEGES",
        0x0002000B: "CONNECT_CANCELLED",
        0x0002000C: "SECURITY_NEGO_CONNECT_FAILED",
        0x0002000D: "CONNECT_TRANSPORT_FAILED",
        0x0002000E: "PASSWORD_EXPIRED",
        0x0002000F: "PASSWORD_CERTAINLY_EXPIRED",
        0x00020014: "LOGON_FAILURE",
        0x00020015: "WRONG_PASSWORD",
        0x00020016: "ACCESS_DENIED",
        0x00020017: "ACCOUNT_RESTRICTION",
        0x00020018: "ACCOUNT_LOCKED_OUT",
        0x00020019: "ACCOUNT_EXPIRED",
        0x0002001A: "ACCOUNT_DISABLED",
    }
    # codes where the server reached the logon and the credentials are the
    # verdict; everything else non-zero is a connection/negotiation problem
    credential = {
        0x00020009, 0x0002000E, 0x0002000F, 0x00020014, 0x00020015,
        0x00020016, 0x00020017, 0x00020018, 0x00020019, 0x0002001A,
    }
    found = [int(value, 16) for value in re.findall(r"0x0002[0-9a-fA-F]{4}", log)]
    nonzero = [code for code in found if code != 0]
    rejected = [code for code in nonzero if code in credential]
    if rejected:
        code = rejected[0]
        return ("AUTH_FAIL", "credentials rejected: ERRCONNECT_%s [0x%08x]"
                % (names.get(code, "UNKNOWN"), code))
    if nonzero:
        code = nonzero[0]
        return ("AUTH_ERROR", "credentials not tested: ERRCONNECT_%s [0x%08x]"
                % (names.get(code, "UNKNOWN"), code))
    if exit_code == 0:
        return ("AUTH_OK", "credentials accepted (auth-only)")
    return ("AUTH_ERROR", "xfreerdp exited %d with no recognised error code" % exit_code)


def _rdp_pick_free_display():
    """Lowest free X display number >= 99, by what already owns a socket.

    Deterministic where ``xvfb-run`` is not: it never shells out to pick a
    server number, so two probes do not race for ``:99`` and then fail with
    "Xvfb failed to start".
    """
    used = set()
    try:
        for name in os.listdir("/tmp/.X11-unix"):
            if name.startswith("X") and name[1:].isdigit():
                used.add(int(name[1:]))
    except OSError:
        pass
    number = 99
    while number in used:
        number += 1
    return number
'''


_RDP_CHECK_MAIN = r'''
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
connection.settimeout(10)

connection.sendall(request)
header = _rdp_recv_exact(connection, 4)
if len(header) < 4:
    print("rdp: short answer, not RDP")
    raise SystemExit(1)
if header[0] != 3:  # TPKT version
    print("rdp: answer is not TPKT/X.224, not RDP")
    raise SystemExit(1)
length = int.from_bytes(header[2:4], "big")
if not 7 <= length <= 65535:
    print("rdp: implausible TPKT length %d, not RDP" % length)
    raise SystemExit(1)
answer = _rdp_recv_exact(connection, length - 4)
if len(answer) < length - 4:
    print("rdp: truncated X.224 answer (%d of %d bytes), not RDP"
          % (len(answer), length - 4))
    raise SystemExit(1)
print("rdp: host answered X.224")

# [0:7] x224 confirmation, then the negotiation structure: type, flags, length, value
lines, selected = rdp_parse_negotiation(answer)
for line in lines:
    print(line)

if selected > 0:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        secured = context.wrap_socket(connection, server_hostname=host)
    except OSError as error:
        print("tls: handshake failed: %s" % error)
        raise SystemExit(0)
    print("tls: handshake ok, %s" % secured.version())
    certificate = secured.getpeercert() or {}
    subject = dict(item[0] for item in certificate.get("subject", ()))
    print("certificate: %s, expires %s" % (
        subject.get("commonName") or subject.get("organizationName") or "unnamed",
        certificate.get("notAfter"),
    ))
raise SystemExit(0)
'''


_RDP_AUTH_MAIN = r'''
def _rdp_start_display():
    """Bring up a deterministic headless X display if the build needs one.

    ``/auth-only`` opens no window, so on most hosts no display is needed and
    this is a no-op. When one is required, a private Xvfb is started on a free
    number and waited for by its socket — not the racy ``xvfb-run`` wrapper that
    flaked with "Xvfb failed to start" on the first attempt in the field.
    """
    if os.environ.get("DISPLAY"):
        return None
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return None
    number = _rdp_pick_free_display()
    process = subprocess.Popen(
        [xvfb, ":%d" % number, "-screen", "0", "16x16x8", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    socket_path = "/tmp/.X11-unix/X%d" % number
    deadline = time.time() + 5
    while time.time() < deadline:
        if os.path.exists(socket_path):
            os.environ["DISPLAY"] = ":%d" % number
            return process
        if process.poll() is not None:
            return None
        time.sleep(0.05)
    process.terminate()
    return None


user = sys.argv[1]
endpoint = sys.argv[-1]
host, port = endpoint.rsplit(":", 1)
password = os.environ.get("SAMURAI_RDP_PASS", "")
binary = shutil.which("xfreerdp3") or shutil.which("xfreerdp")
if not binary:
    print("rdp-auth: xfreerdp not found on PATH")
    raise SystemExit(3)

display = _rdp_start_display()
# the password rides in through stdin via /args-from, never on argv, so it does
# not show in the host process list (xfreerdp masks /p: only after a race)
arguments = "\n".join([
    "/v:%s:%s" % (host, port),
    "/u:%s" % user,
    "/p:%s" % password,
    "/cert:ignore",
    "/auth-only",
    "/timeout:10000",
])
try:
    completed = subprocess.run(
        [binary, "/args-from:stdin"],
        input=arguments, text=True, capture_output=True, timeout=45,
    )
    output, errors, code = completed.stdout, completed.stderr, completed.returncode
except subprocess.TimeoutExpired:
    output, errors, code = "", "xfreerdp timed out", 3
finally:
    if display is not None:
        display.terminate()

sys.stdout.write(output)
if errors:
    sys.stderr.write(errors)
verdict, detail = rdp_auth_verdict(code, output + "\n" + errors)
print("rdp-auth: %s (%s)" % (verdict, detail))
raise SystemExit({"AUTH_OK": 0, "AUTH_FAIL": 2, "AUTH_ERROR": 3}[verdict])
'''


_RDP_DRIVER = "import os, re, socket, ssl, sys\n" + _RDP_SHARED + _RDP_CHECK_MAIN
_RDP_AUTH_DRIVER = (
    "import os, re, shutil, socket, subprocess, sys, time\n"
    + _RDP_SHARED
    + _RDP_AUTH_MAIN
)


class RdpAuthTool(_RemoteTool):
    """Validate a username/password against an in-scope RDP host.

    Unlike :class:`RdpCheckTool` — a read-only X.224/TLS probe that only reports
    what security the host asks for — this attempts authentication with
    ``xfreerdp /auth-only``, so it is R3 and not read-only. ``/auth-only``
    negotiates security and performs the logon but never opens a window, so no
    interactive session is created and, on most hosts, no X display is needed; a
    deterministic Xvfb is started only if the local xfreerdp build insists on
    one, never the racy ``xvfb-run``. The verdict is read from xfreerdp's exit
    code and its ``ERRCONNECT`` last-error blob — not the ambiguous
    ``Authentication only, exit status N`` log line — so a cancelled or errored
    connection is never reported as valid credentials.
    """

    default_port = 3389

    def __init__(self, runner: DockerSandboxRunner | None = None) -> None:
        super().__init__(runner)
        self._definition = ToolDefinition(
            id="remote.rdp-auth",
            version=1,
            kind=ToolKind.SANDBOX,
            description="Check whether a username/password is accepted by an "
            "in-scope RDP host (xfreerdp /auth-only), without opening a session; "
            "distinguishes valid credentials from a rejected logon and from a "
            "connection that never reached the logon",
            risk_class=RiskClass.R3,
            network=NetworkAccess.PROXIED,
            timeout_seconds=120,
            output_limit_bytes=65536,
            parameters={
                "type": "object",
                "properties": {
                    **_HOST_PORT,
                    "port": {"type": "integer", "default": 3389},
                    "user": {"type": "string", "minLength": 1, "maxLength": 256},
                    "password": {"type": "string", "minLength": 0, "maxLength": 1024},
                },
                "required": ["host", "user", "password"],
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
        # the password rides in the secret env (redacted, off the process list);
        # the driver pipes it to xfreerdp via /args-from, never on argv. Keep the
        # driver off the env proxy too — the tunnel already is the proxy.
        return {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "NO_PROXY": "host.docker.internal",
            "no_proxy": "host.docker.internal",
        }, {"SAMURAI_RDP_PASS": str(params["password"])}

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        # pass the full params through: build_env needs the password, and
        # _host_port already applies the 3389 default for the tunnel
        return self._run(["-c", _RDP_AUTH_DRIVER, str(params["user"])], params, scope)


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


__all__ = [
    "AdbTool",
    "RdpAuthTool",
    "RdpCheckTool",
    "SmbListTool",
    "SshExecTool",
    "SshPassExecTool",
]
