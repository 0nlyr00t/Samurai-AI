from pathlib import Path

import pytest

from samurai_ai.domain.execution import ApprovalRequiredError, ToolRunStatus
from samurai_ai.domain.kernel import ArtifactKind
from samurai_ai.domain.operations import Operation, OperationMode
from samurai_ai.domain.sandbox import SandboxError
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import SandboxExecTool, ToolRegistry
from samurai_ai.execution.sandbox import (
    DockerSandboxRunner,
    get_profile,
    sign_allowlist,
)
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
PROFILE_ID = "sandbox.default@v1"


class FakeManager:
    stall_seconds: float = 120.0

    def __init__(self, run_result: ProcessResult | None = None) -> None:
        self.run_result = run_result or ProcessResult(
            exit_code=0, stdout="sandboxed-output", stderr=""
        )
        self.calls: list[tuple[str, ...]] = []

    async def run(
        self, argv: list[str] | tuple[str, ...], timeout_seconds: float | None = None
    ) -> ProcessResult:
        arguments = tuple(argv)
        self.calls.append(arguments)
        if arguments[:2] == ("docker", "run"):
            return self.run_result
        return ProcessResult(exit_code=0, stdout="ok", stderr="")


def make_stack(
    database_path: Path, manager: FakeManager
) -> tuple[OperationService, KernelService, ToolExecutionService]:
    database = Database(database_path)
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    runner = DockerSandboxRunner(
        manager, database_path.parent / "sandbox-allowlist.sig", lambda: MASTER_KEY
    )
    registry = ToolRegistry(adapters=(SandboxExecTool(runner),))
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), registry=registry
    )
    return service, kernel, execution


def make_operation(service: OperationService) -> Operation:
    return service.create(
        "Sandbox lab",
        OperationMode.BUG_BOUNTY,
        ScopeContract(includes=("example.com",)),
    )


def test_sandbox_exec_signs_a_missing_allowlist(tmp_path: Path) -> None:
    manager = FakeManager()
    service, _, execution = make_stack(tmp_path / "unsigned.db", manager)
    operation = make_operation(service)
    with pytest.raises(ApprovalRequiredError):
        execution.run_tool(
            operation.id,
            "sandbox.exec",
            {"profile": PROFILE_ID, "executable": "echo"},
        )
    # the run gets past the signature check and stops at the approval prompt
    assert (tmp_path / "sandbox-allowlist.sig").exists()
    assert execution.list_runs(operation.id) == []
    assert not [call for call in manager.calls if call[:2] == ("docker", "run")]


def test_sandbox_exec_fail_closed_when_docker_unavailable(tmp_path: Path) -> None:
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)

    class NoDocker(FakeManager):
        async def run(self, argv, timeout_seconds=None):  # type: ignore[no-untyped-def]
            arguments = tuple(argv)
            self.calls.append(arguments)
            if arguments[:2] == ("docker", "version"):
                return ProcessResult(exit_code=1, stdout="", stderr="daemon down")
            return ProcessResult(exit_code=0, stdout="ok", stderr="")

    manager = NoDocker()
    service, _, execution = make_stack(tmp_path / "nodocker.db", manager)
    operation = make_operation(service)
    with pytest.raises(SandboxError, match="docker is not available"):
        execution.run_tool(
            operation.id,
            "sandbox.exec",
            {"profile": PROFILE_ID, "executable": "echo"},
        )
    assert execution.list_runs(operation.id) == []
    assert not [call for call in manager.calls if call[:2] == ("docker", "run")]


def test_sandbox_exec_approval_flow_and_artifacts(tmp_path: Path) -> None:
    database_path = tmp_path / "full.db"
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    manager = FakeManager()
    service, kernel, execution = make_stack(database_path, manager)
    operation = make_operation(service)

    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(
            operation.id,
            "sandbox.exec",
            {"profile": PROFILE_ID, "executable": "echo", "args": ["hello sandbox"]},
        )
    assert execution.list_runs(operation.id) == []

    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id,
        "sandbox.exec",
        {"profile": PROFILE_ID, "executable": "echo", "args": ["hello sandbox"]},
        approval_id=caught.value.approval_id,
    )
    assert result.run.status == ToolRunStatus.COMPLETED
    assert result.stdout == "sandboxed-output"
    assert result.run.approval_id == caught.value.approval_id
    assert result.run.recovery_action_id
    assert result.run.parameters == {
        "profile": PROFILE_ID,
        "executable": "echo",
        "args": ["hello sandbox"],
    }

    run_calls = [call for call in manager.calls if call[:2] == ("docker", "run")]
    assert len(run_calls) == 1
    argv = run_calls[0]
    assert argv[argv.index("--name") + 1] == f"samurai-{result.run.id}"
    assert argv[argv.index("--network") + 1] == "none"

    assert result.run.stdout_artifact_id is not None
    artifact = kernel.kernel.get_artifact(result.run.stdout_artifact_id)
    assert artifact is not None
    assert artifact.kind == ArtifactKind.COMMAND_OUTPUT
    content = (database_path.parent / artifact.path).read_text(encoding="utf-8")
    assert content == "sandboxed-output"


def test_sandbox_exec_redacts_vault_secrets_in_output(tmp_path: Path) -> None:
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    secret = "sandbox-secret-value-71"
    manager = FakeManager(
        ProcessResult(exit_code=0, stdout=f"leaked {secret} here", stderr="")
    )
    service, kernel, execution = make_stack(tmp_path / "redact.db", manager)
    operation = make_operation(service)
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(
            operation.id,
            "sandbox.exec",
            {"profile": PROFILE_ID, "executable": "echo"},
        )
    kernel.approve(caught.value.approval_id, decided_by="operator")
    execution.redactor.register(secret)
    result = execution.run_tool(
        operation.id,
        "sandbox.exec",
        {"profile": PROFILE_ID, "executable": "echo"},
        approval_id=caught.value.approval_id,
    )
    assert secret not in result.stdout
    assert result.redaction_count >= 1


def test_enforce_output_limit_keeps_the_stalled_flag() -> None:
    """A stalled run must not lose its flag when the output cap is applied.

    The flag is how a hung ssh/shell is told apart from a clean exit downstream;
    rebuilding the result used to drop it, so the stall vanished silently.
    """
    stalled = ProcessResult(
        exit_code=-15, stdout="partial", stderr="", stalled=True
    )
    kept = DockerSandboxRunner._enforce_output_limit(get_profile(PROFILE_ID), stalled)
    assert kept.stalled is True


def test_a_stalled_sandbox_run_is_recorded_as_a_timeout(tmp_path: Path) -> None:
    """A hung command (killed on the stall watchdog) lands as TIMEOUT, not FAILED.

    Recording it as a timeout is what lets the repeat-guard dedupe the hung
    command instead of the agent re-running the same dead transport.
    """
    sign_allowlist(tmp_path / "sandbox-allowlist.sig", MASTER_KEY)
    # the watchdog kills the group, so the exit code is a signal, but the stall
    # is the real cause — it must win over the raw exit code
    manager = FakeManager(
        ProcessResult(exit_code=-15, stdout="", stderr="", stalled=True)
    )
    service, kernel, execution = make_stack(tmp_path / "stall.db", manager)
    operation = make_operation(service)
    with pytest.raises(ApprovalRequiredError) as caught:
        execution.run_tool(
            operation.id,
            "sandbox.exec",
            {"profile": PROFILE_ID, "executable": "echo"},
        )
    kernel.approve(caught.value.approval_id, decided_by="operator")
    result = execution.run_tool(
        operation.id,
        "sandbox.exec",
        {"profile": PROFILE_ID, "executable": "echo"},
        approval_id=caught.value.approval_id,
    )
    assert result.run.status == ToolRunStatus.TIMEOUT
    assert "stalled" in result.stderr
