"""Remote admin tools: scope first, tunnel to the target, argv shape."""

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ToolScopeError, ToolValidationError
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import ToolRegistry
from samurai_ai.execution.remote import (
    AdbTool,
    RdpAuthTool,
    RdpCheckTool,
    SmbListTool,
    SshExecTool,
    SshPassExecTool,
)
from samurai_ai.execution.sandbox import DockerSandboxRunner, sign_allowlist
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository

MASTER_KEY = bytes.fromhex("ab" * 32)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def listener() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class FakeManager:
    """Behaves like docker: preflight succeeds, the run returns canned output."""

    stall_seconds: float = 120.0

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, timeout_seconds=None, **kwargs):  # type: ignore[no-untyped-def]
        arguments = tuple(argv)
        self.calls.append(arguments)
        if arguments[:2] in (("docker", "version"), ("docker", "image")):
            return ProcessResult(exit_code=0, stdout="ok", stderr="")
        self._touch_tunnel(list(arguments))
        return ProcessResult(exit_code=0, stdout="ran: " + " ".join(arguments[-4:]), stderr="")

    def _touch_tunnel(self, argv: list[str]) -> None:
        """Actually dial the tunnel so real egress events are produced."""
        try:
            endpoint = argv[-1]
            _, _, port = endpoint.partition(":")
            with socket.create_connection(("127.0.0.1", int(port)), timeout=2) as conn:
                conn.sendall(b"x")
                conn.settimeout(0.5)
                try:
                    conn.recv(16)
                except OSError:
                    pass
        except (ValueError, IndexError, OSError):
            pass


def make_stack(
    tmp_path: Path, adapters: tuple[object, ...]
) -> tuple[OperationService, KernelService, ToolExecutionService, FakeManager]:
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    manager = FakeManager()
    database = Database(tmp_path / "remote.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    runner = DockerSandboxRunner(
        manager, tmp_path / "sandbox-allowlist.sig", lambda: MASTER_KEY
    )
    registry = ToolRegistry(adapters=tuple(a(runner) for a in adapters))  # type: ignore[arg-type]
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), registry=registry
    )
    return service, kernel, execution, manager


def make_operation(service: OperationService) -> Operation:
    return service.create(
        "Remote lab", OperationMode.RED_TEAM, ScopeContract(includes=("127.0.0.1",))
    )


def approve(kernel: KernelService, operation_id: str, tool_id: str, risk: RiskClass) -> str:
    approval = kernel.request_approval(operation_id, f"tool.run {tool_id}", risk)
    kernel.approve(approval.id, decided_by="operator")
    return approval.id


def docker_run_argv(manager: FakeManager) -> list[str]:
    runs = [list(call) for call in manager.calls if call[:2] == ("docker", "run")]
    assert len(runs) == 1
    return runs[0]


def test_ssh_exec_goes_through_a_tunnel(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (SshExecTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.ssh-exec", RiskClass.R3)
    result = execution.run_tool(
        operation.id,
        "remote.ssh-exec",
        {
            "host": "127.0.0.1",
            "port": listener,
            "user": "root",
            "command": "id",
            "key": "-----BEGIN KEY-----\nabc\n-----END KEY-----",
        },
        approval_id=approval_id,
    )
    assert result.run.status.value == "completed"
    argv = docker_run_argv(manager)
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    # the tunnel endpoint, not the real target, is what the tool is handed
    endpoint = argv[-1]
    assert endpoint.startswith("host.docker.internal:")
    assert "[egress: 127.0.0.1 allowed;" in result.stderr


def test_ssh_pass_goes_through_a_tunnel(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (SshPassExecTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.ssh-pass", RiskClass.R3)
    result = execution.run_tool(
        operation.id,
        "remote.ssh-pass",
        {
            "host": "127.0.0.1",
            "port": listener,
            "user": "contractor",
            "command": "id",
            "password": "Contractor2026!",
        },
        approval_id=approval_id,
    )
    assert result.run.status.value == "completed"
    argv = docker_run_argv(manager)
    assert argv[argv.index("--entrypoint") + 1] == "python3"
    # the driver, not the plaintext password, is what reaches docker's argv
    assert "Contractor2026!" not in " ".join(argv)
    endpoint = argv[-1]
    assert endpoint.startswith("host.docker.internal:")
    assert "[egress: 127.0.0.1 allowed;" in result.stderr


def test_ssh_pass_password_may_come_from_the_vault(
    tmp_path: Path, listener: int
) -> None:
    """A ``{{secret:...}}`` password resolves and never appears in the output."""
    from samurai_ai.services.vault import VaultService
    from samurai_ai.storage.vault import VaultRepository

    service, kernel, execution, manager = make_stack(tmp_path, (SshPassExecTool,))
    operation = make_operation(service)
    vault = VaultService(
        VaultRepository(Database(tmp_path / "remote.db")),
        service.repository,
        tmp_path / "vault.key",
    )
    vault.add("winpass", "Contractor2026!", operation.id)
    execution.vault = vault
    approval_id = approve(kernel, operation.id, "remote.ssh-pass", RiskClass.R3)
    result = execution.run_tool(
        operation.id,
        "remote.ssh-pass",
        {
            "host": "127.0.0.1",
            "port": listener,
            "user": "contractor",
            "command": "id",
            "password": "{{secret:winpass}}",
        },
        approval_id=approval_id,
    )
    assert result.run.status.value == "completed"
    assert "Contractor2026!" not in result.stdout
    assert "Contractor2026!" not in result.stderr


def test_ssh_exec_falls_back_to_native_when_docker_is_absent(
    tmp_path: Path, listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Docker: the driver runs on the host under bwrap, via the same tunnel."""
    from samurai_ai.execution import remote

    native_calls: list[tuple[str, ...]] = []

    class FakeNative:
        stall_seconds = 120.0

        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self, argv, timeout_seconds=None, **kwargs):  # type: ignore[no-untyped-def]
            native_calls.append(tuple(argv))
            endpoint = argv[-1]
            _, _, port = endpoint.partition(":")
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=2) as conn:
                    conn.sendall(b"x")
                    conn.settimeout(0.5)
                    try:
                        conn.recv(16)
                    except OSError:
                        pass
            except (ValueError, OSError):
                pass
            return ProcessResult(exit_code=0, stdout="native ok", stderr="")

    monkeypatch.setattr(remote, "ProcessManager", FakeNative)
    monkeypatch.setattr(remote, "bwrap_available", lambda: True)

    class NoDocker(FakeManager):
        async def run(self, argv, timeout_seconds=None, **kwargs):  # type: ignore[no-untyped-def]
            if tuple(argv)[:2] == ("docker", "version"):
                return ProcessResult(exit_code=1, stdout="", stderr="no docker")
            return await FakeManager.run(self, argv, timeout_seconds, **kwargs)

    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    manager = NoDocker()
    database = Database(tmp_path / "remote.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    runner = DockerSandboxRunner(
        manager, tmp_path / "sandbox-allowlist.sig", lambda: MASTER_KEY
    )
    registry = ToolRegistry(adapters=(SshExecTool(runner),))
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), registry=registry
    )
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.ssh-exec", RiskClass.R3)
    result = execution.run_tool(
        operation.id,
        "remote.ssh-exec",
        {
            "host": "127.0.0.1",
            "port": listener,
            "user": "root",
            "command": "id",
            "key": "-----BEGIN KEY-----\nabc\n-----END KEY-----",
        },
        approval_id=approval_id,
    )
    assert result.run.status.value == "completed"
    assert native_calls, "the native manager must run when docker is absent"
    argv = list(native_calls[0])
    assert argv[0] == "bwrap"  # confined by bubblewrap, not the container
    assert argv[-1].startswith("127.0.0.1:")  # the localhost tunnel, not the docker alias
    assert "bubblewrap" in result.stderr  # the native note is surfaced
    assert "native ok" in result.stdout
    assert "[egress: 127.0.0.1 allowed;" in result.stderr


def test_remote_tools_check_scope_before_spawn(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (SmbListTool,))
    operation = service.create(
        "Out of scope", OperationMode.RED_TEAM, ScopeContract(includes=("example.com",))
    )
    approval_id = approve(kernel, operation.id, "remote.smb-list", RiskClass.R2)
    with pytest.raises(ToolScopeError):
        execution.run_tool(
            operation.id, "remote.smb-list", {"host": "127.0.0.1", "port": listener},
            approval_id=approval_id,
        )
    assert not [call for call in manager.calls if call[:2] == ("docker", "run")]


def test_rdp_check_forces_the_rdp_port(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (RdpCheckTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.rdp-check", RiskClass.R2)
    execution.run_tool(
        operation.id,
        "remote.rdp-check",
        {"host": "127.0.0.1", "port": listener},
        approval_id=approval_id,
    )
    argv = docker_run_argv(manager)
    assert argv[-1].startswith("host.docker.internal:")


def test_rdp_auth_tunnels_and_hides_the_password(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (RdpAuthTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.rdp-auth", RiskClass.R3)
    result = execution.run_tool(
        operation.id,
        "remote.rdp-auth",
        {
            "host": "127.0.0.1",
            "port": listener,
            "user": "contractor",
            "password": "Contractor2026!",
        },
        approval_id=approval_id,
    )
    assert result.run.status.value == "completed"
    argv = docker_run_argv(manager)
    # the endpoint the driver sees is the tunnel, not the real target
    assert argv[-1].startswith("host.docker.internal:")
    # the password reaches xfreerdp through the secret env, never docker's argv
    assert "Contractor2026!" not in " ".join(argv)
    assert "[egress: 127.0.0.1 allowed;" in result.stderr


def test_adb_needs_an_argument_for_shell(tmp_path: Path, listener: int) -> None:
    service, kernel, execution, manager = make_stack(tmp_path, (AdbTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.adb", RiskClass.R3)
    with pytest.raises(ToolValidationError, match="needs an argument"):
        execution.run_tool(
            operation.id,
            "remote.adb",
            {"host": "127.0.0.1", "port": listener, "action": "shell"},
            approval_id=approval_id,
        )
    assert not [call for call in manager.calls if call[:2] == ("docker", "run")]


def test_the_tunnel_reports_volume_in_the_warning_line(tmp_path: Path, listener: int) -> None:
    """The audit line must carry what flowed, not only the verdict."""
    service, kernel, execution, manager = make_stack(tmp_path, (SmbListTool,))
    operation = make_operation(service)
    approval_id = approve(kernel, operation.id, "remote.smb-list", RiskClass.R2)
    result = execution.run_tool(
        operation.id,
        "remote.smb-list",
        {"host": "127.0.0.1", "port": listener},
        approval_id=approval_id,
    )
    assert "allowed;" in result.stderr
    completed = [
        event
        for event in service.events(operation.id)
        if event.event_type == "tool.run.completed"
    ]
    assert completed[0].payload["egress"][0]["verdict"] == "allowed"
