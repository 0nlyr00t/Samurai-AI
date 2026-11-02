import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import SandboxExecTool, ToolRegistry
from samurai_ai.execution.sandbox import (
    DockerSandboxRunner,
    get_profile,
    sign_allowlist,
)
from samurai_ai.execution.security import DnsxResolveTool, NmapScanTool
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.vault import load_master_key
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("SAMURAI_TEST_DOCKER") != "1",
    reason="opt-in docker sandbox test (set SAMURAI_TEST_DOCKER=1)",
)


def test_sandbox_exec_runs_in_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.default@v1")
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )
    if not asyncio.run(runner.docker_available()):
        pytest.skip("docker daemon is not available")
    if not asyncio.run(runner.image_present(profile.image)):
        pytest.skip(f"sandbox image not pulled: {profile.image}")

    sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))

    database = Database(tmp_path / "docker.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database)
    )
    operation = service.create(
        "Docker sandbox lab",
        OperationMode.CTF_HTB,
        ScopeContract(includes=("example.com",)),
    )
    params = {"profile": profile.id, "executable": "echo", "args": ["hello from sandbox"]}
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "sandbox.exec", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "sandbox.exec", params, approval_id=caught.value.approval_id
    )
    assert result.run.status == ToolRunStatus.COMPLETED
    assert result.stdout.strip() == "hello from sandbox"


def test_secret_env_reaches_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.scoped@v1")
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )
    if not asyncio.run(runner.docker_available()):
        pytest.skip("docker daemon is not available")
    if not asyncio.run(runner.image_present(profile.image)):
        pytest.skip(f"sandbox image not pulled: {profile.image}")
    sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))

    secret = "s3cr3t-channel-value"
    outcome = runner.run(
        profile,
        "run_env_test",
        "cat",
        ["/proc/self/environ"],
        ScopeContract(includes=("example.com",)),
        env={"HOME": "/tmp"},
        secret_env={"SAMURAI_TEST_SECRET": secret},
    )
    environment = outcome.result.stdout.split("\x00")
    assert f"SAMURAI_TEST_SECRET={secret}" in environment
    assert "HOME=/tmp" in environment


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"scoped-docker-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_scoped_sandbox_egress_through_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.scoped@v1")
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
        egress_allowed_ports=frozenset({80, 443, 18999}),
    )
    if not asyncio.run(runner.docker_available()):
        pytest.skip("docker daemon is not available")
    if not asyncio.run(runner.image_present(profile.image)):
        pytest.skip(f"sandbox image not pulled: {profile.image}")

    server = HTTPServer(("127.0.0.1", 18999), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))
        database = Database(tmp_path / "scoped-docker.db")
        service = OperationService(OperationRepository(database))
        kernel = KernelService(service, KernelRepository(database))
        recovery = RecoveryService(service, kernel, RecoveryRepository(database))
        execution = ToolExecutionService(
            service,
            kernel,
            recovery,
            ToolRunRepository(database),
            registry=ToolRegistry(adapters=(SandboxExecTool(runner),)),
        )
        operation = service.create(
            "Scoped docker lab",
            OperationMode.CTF_HTB,
            ScopeContract(includes=("localhost",)),
        )
        params = {
            "profile": profile.id,
            "executable": "wget",
            "args": ["-q", "-O-", "-T", "10", "http://localhost:18999/"],
        }
        with pytest.raises(ApprovalRequiredError) as caught:
            execution.run_tool(operation.id, "sandbox.exec", params)
        kernel.approve(caught.value.approval_id, decided_by="operator")
        result = execution.run_tool(
            operation.id, "sandbox.exec", params, approval_id=caught.value.approval_id
        )
        assert result.run.status == ToolRunStatus.COMPLETED
        assert result.stdout.strip() == "scoped-docker-ok"

        events = service.events(operation.id)
        completed = [e for e in events if e.event_type == "tool.run.completed"]
        egress = completed[0].payload.get("egress", [])
        assert egress and egress[0]["verdict"] == "allowed"
        assert egress[0]["host"] == "localhost"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_dnsx_resolves_through_socks5_and_doh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.dnsx@v1")
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )
    if not asyncio.run(runner.docker_available()):
        pytest.skip("docker daemon is not available")
    if not asyncio.run(runner.image_present(profile.image)):
        pytest.skip(f"sandbox image not pulled: {profile.image}")
    sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))

    database = Database(tmp_path / "dnsx-docker.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=ToolRegistry(adapters=(DnsxResolveTool(runner),)),
    )
    operation = service.create(
        "DNS docker lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com", "*.example.com")),
    )
    params: dict = {"hosts": ["example.com", "www.example.com"]}
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "recon.dnsx-resolve", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id,
        "recon.dnsx-resolve",
        params,
        approval_id=caught.value.approval_id,
    )
    assert result.run.status is ToolRunStatus.COMPLETED
    assert "NOERROR" in result.stdout
    assert "A=" in result.stdout
    completed = [
        event
        for event in service.events(operation.id)
        if event.event_type == "tool.run.completed"
    ]
    egress = completed[0].payload.get("egress", [])
    assert egress and all(item["host"] == "cloudflare-dns.com" for item in egress)
    assert all(item["verdict"] == "allowed" for item in egress)


def test_nmap_scan_finds_an_open_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.nmap@v1")
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: load_master_key(vault_key_path()),
    )
    if not asyncio.run(runner.docker_available()):
        pytest.skip("docker daemon is not available")
    if not asyncio.run(runner.image_present(profile.image)):
        pytest.skip(f"sandbox image not pulled: {profile.image}")
    sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))

    database = Database(tmp_path / "nmap-docker.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=ToolRegistry(adapters=(NmapScanTool(runner),)),
    )
    operation = service.create(
        "nmap docker lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    params: dict = {"targets": ["example.com"], "ports": "443"}
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "scan.nmap", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "scan.nmap", params, approval_id=caught.value.approval_id
    )
    assert result.run.status is ToolRunStatus.COMPLETED
    summary = result.stdout.split("# raw")[0]
    assert ":443/tcp open" in summary
