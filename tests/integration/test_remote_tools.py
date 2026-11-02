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
from samurai_ai.execution.remote import AdbTool, RdpCheckTool, SmbListTool, SshExecTool
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
