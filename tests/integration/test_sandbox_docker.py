import asyncio
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.registry import SandboxExecTool, ToolRegistry
from samurai_ai.execution.remote import (
    RdpCheckTool,
    SmbListTool,
    SshExecTool,
)
from samurai_ai.execution.sandbox import (
    DockerSandboxRunner,
    get_profile,
    sign_allowlist,
)
from samurai_ai.execution.security import (
    DnsxResolveTool,
    FfufFuzzTool,
    NmapScanTool,
    ResearchReadTool,
)
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.services.traffic import TrafficService
from samurai_ai.services.vault import load_master_key
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository
from samurai_ai.storage.traffic import TrafficRepository

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


def test_ffuf_fuzzes_through_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.ffuf@v1")
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

    database = Database(tmp_path / "ffuf-docker.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=ToolRegistry(adapters=(FfufFuzzTool(runner),)),
    )
    operation = service.create(
        "ffuf docker lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    params: dict = {"url": "https://example.com/FUZZ"}
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "fuzz.ffuf", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "fuzz.ffuf", params, approval_id=caught.value.approval_id
    )
    assert result.run.status is ToolRunStatus.COMPLETED
    # every request went through our proxy, none straight out
    completed = [
        event
        for event in service.events(operation.id)
        if event.event_type == "tool.run.completed"
    ]
    egress = completed[0].payload.get("egress", [])
    assert egress and all(item["host"] == "example.com" for item in egress)
    assert all(item["verdict"] == "allowed" for item in egress)
    # example.com answers the same for any path, so calibration filters it all
    assert "# summary" in result.stdout


def test_research_read_renders_through_the_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
    profile = get_profile("sandbox.camoufox@v1")
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

    database = Database(tmp_path / "research-docker.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    traffic = TrafficService(service, kernel, TrafficRepository(database))
    execution = ToolExecutionService(
        service,
        kernel,
        recovery,
        ToolRunRepository(database),
        registry=ToolRegistry(adapters=(ResearchReadTool(runner),)),
        traffic=traffic,
    )
    operation = service.create(
        "research docker lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )
    params: dict = {"url": "https://example.com/", "max_chars": 500}
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(operation.id, "research.read", params)
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id, "research.read", params, approval_id=caught.value.approval_id
    )
    assert result.run.status is ToolRunStatus.COMPLETED
    assert "title: Example Domain" in result.stdout
    # what the browser read is now an evidence pair
    pairs = traffic.for_operation(operation.id)
    assert pairs and pairs[0].url == "https://example.com/"
    assert pairs[0].created_by == "tool:research.read"


SSHD_IMAGE = (
    "linuxserver/openssh-server@sha256:"
    "0b46c755aac07057908077f2c7ff14f0ad923970bb6a3a67bcd008e72fa60050"
)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_ssh_exec_reaches_a_real_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tunnel is only proven by a real sshd: start one, ssh into it."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if asyncio.run(_docker_available()) is False:
        pytest.skip("docker daemon is not available")
    if asyncio.run(_image_present(SSHD_IMAGE)) is False:
        pytest.skip(f"target image not pulled: {SSHD_IMAGE}")

    key_path = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
    )
    port = _free_port()
    name = f"samurai-sshd-{port}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"{port}:2222",
            "-e", "PUID=1000", "-e", "PGID=1000",
            "-e", f"PUBLIC_KEY={key_path.with_suffix('.pub').read_text().strip()}",
            "-e", "USER_NAME=ops", SSHD_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        assert _wait_for_banner(port), "the target sshd never answered"
        monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
        sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))
        database = Database(tmp_path / "ssh.db")
        service = OperationService(OperationRepository(database))
        kernel = KernelService(service, KernelRepository(database))
        recovery = RecoveryService(service, kernel, RecoveryRepository(database))
        runner = DockerSandboxRunner(
            ProcessManager(allowed_executables=frozenset({"docker"})),
            sandbox_allowlist_path(),
            lambda: load_master_key(vault_key_path()),
        )
        execution = ToolExecutionService(
            service,
            kernel,
            recovery,
            ToolRunRepository(database),
            registry=ToolRegistry(adapters=(SshExecTool(runner),)),
        )
        operation = service.create(
            "ssh lab", OperationMode.RED_TEAM, ScopeContract(includes=("127.0.0.1",))
        )
        params = {
            "host": "127.0.0.1",
            "port": port,
            "user": "ops",
            "command": "id -un",
            "key": key_path.read_text(),
        }
        with pytest.raises(ApprovalRequiredError) as caught:
            execution.run_tool(operation.id, "remote.ssh-exec", params)
        kernel.approve(caught.value.approval_id, decided_by="operator")
        result = execution.run_tool(
            operation.id, "remote.ssh-exec", params, approval_id=caught.value.approval_id
        )
        assert result.run.status is ToolRunStatus.COMPLETED, result.stderr[-400:]
        assert "ops" in result.stdout
        assert "allowed" in result.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _wait_for_banner(port: int, timeout: float = 30.0) -> bool:
    """An open port is not an sshd yet: wait until it actually greets us."""
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
                if conn.recv(16).startswith(b"SSH-"):
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


async def _docker_available() -> bool:
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: b"x" * 32,
    )
    return await runner.docker_available()


async def _image_present(image: str) -> bool:
    runner = DockerSandboxRunner(
        ProcessManager(allowed_executables=frozenset({"docker"})),
        sandbox_allowlist_path(),
        lambda: b"x" * 32,
    )
    return await runner.image_present(image)


SMB_IMAGE = (
    "dperson/samba@sha256:"
    "66088b78a19810dd1457a8f39340e95e663c728083efa5fe7dc0d40b2478e869"
)


def test_smb_list_reaches_a_real_samba(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMB rides the tunnel too: list shares of a real Samba server."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if asyncio.run(_docker_available()) is False:
        pytest.skip("docker daemon is not available")
    if asyncio.run(_image_present(SMB_IMAGE)) is False:
        pytest.skip(f"target image not pulled: {SMB_IMAGE}")

    port = _free_port()
    name = f"samurai-smb-{port}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"{port}:445", SMB_IMAGE,
            "-p", "-s", "share;/mnt;yes;no;no;all", "-u", "ops;ops123",
        ],
        check=True,
        capture_output=True,
    )
    try:
        assert _wait_for_port(port)
        monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
        sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))
        database = Database(tmp_path / "smb.db")
        service = OperationService(OperationRepository(database))
        kernel = KernelService(service, KernelRepository(database))
        recovery = RecoveryService(service, kernel, RecoveryRepository(database))
        runner = DockerSandboxRunner(
            ProcessManager(allowed_executables=frozenset({"docker"})),
            sandbox_allowlist_path(),
            lambda: load_master_key(vault_key_path()),
        )
        execution = ToolExecutionService(
            service,
            kernel,
            recovery,
            ToolRunRepository(database),
            registry=ToolRegistry(adapters=(SmbListTool(runner),)),
        )
        operation = service.create(
            "smb lab", OperationMode.RED_TEAM, ScopeContract(includes=("127.0.0.1",))
        )
        params = {"host": "127.0.0.1", "port": port}
        with pytest.raises(ApprovalRequiredError) as caught:
            execution.run_tool(operation.id, "remote.smb-list", params)
        kernel.approve(caught.value.approval_id, decided_by="operator")
        result = execution.run_tool(
            operation.id, "remote.smb-list", params, approval_id=caught.value.approval_id
        )
        assert result.run.status is ToolRunStatus.COMPLETED, result.stderr[-300:]
        assert "share" in result.stdout
        assert "allowed" in result.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _wait_for_log(name: str, needle: str, timeout: float = 90.0) -> bool:
    """RDP negotiates before TLS, so a port or TLS probe are both premature."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "logs", name], capture_output=True, text=True
        )
        if needle in logs.stdout + logs.stderr:
            return True
        time.sleep(1.0)
    return False


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


RDP_IMAGE = (
    "danielguerra/ubuntu-xrdp@sha256:"
    "1a00da32f4e486f2f5fd8f656fc23bb235987c219153a587894469cad300b12d"
)


def test_rdp_check_reaches_a_real_xrdp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RDP rides the tunnel: an xrdp server must answer the auth-only probe."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if asyncio.run(_docker_available()) is False:
        pytest.skip("docker daemon is not available")
    if asyncio.run(_image_present(RDP_IMAGE)) is False:
        pytest.skip(f"target image not pulled: {RDP_IMAGE}")

    port = _free_port()
    name = f"samurai-rdp-{port}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "--platform", "linux/amd64", "-p", f"{port}:3389", RDP_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        # the port opens before xrdp is ready: wait for its own "running" log
        assert _wait_for_log(name, "xrdp entered RUNNING state", timeout=90)
        monkeypatch.setenv("SAMURAI_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("SAMURAI_VAULT_KEY", "ab" * 32)
        sign_allowlist(sandbox_allowlist_path(), load_master_key(vault_key_path()))
        database = Database(tmp_path / "rdp.db")
        service = OperationService(OperationRepository(database))
        kernel = KernelService(service, KernelRepository(database))
        recovery = RecoveryService(service, kernel, RecoveryRepository(database))
        runner = DockerSandboxRunner(
            ProcessManager(allowed_executables=frozenset({"docker"})),
            sandbox_allowlist_path(),
            lambda: load_master_key(vault_key_path()),
        )
        execution = ToolExecutionService(
            service,
            kernel,
            recovery,
            ToolRunRepository(database),
            registry=ToolRegistry(adapters=(RdpCheckTool(runner),)),
        )
        operation = service.create(
            "rdp lab", OperationMode.RED_TEAM, ScopeContract(includes=("127.0.0.1",))
        )
        params = {"host": "127.0.0.1", "port": port}
        with pytest.raises(ApprovalRequiredError) as caught:
            execution.run_tool(operation.id, "remote.rdp-check", params)
        kernel.approve(caught.value.approval_id, decided_by="operator")
        result = execution.run_tool(
            operation.id, "remote.rdp-check", params, approval_id=caught.value.approval_id
        )
        assert result.run.status is ToolRunStatus.COMPLETED, result.stderr[-300:]
        assert "exit status 0" in (result.stdout + result.stderr)
        assert "allowed" in result.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
