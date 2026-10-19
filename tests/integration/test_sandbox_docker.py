import asyncio
import os
from pathlib import Path

import pytest

from samurai_ai.config import sandbox_allowlist_path, vault_key_path
from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager
from samurai_ai.execution.sandbox import (
    DockerSandboxRunner,
    get_profile,
    sign_allowlist,
)
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
