"""Operator rules hold at the execution boundary, whichever caller arrives."""

from pathlib import Path

import pytest

from samurai_ai.domain.execution import ApprovalRequiredError, ToolDeniedError
from samurai_ai.domain.kernel import ApprovalStatus
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.permission import PermissionRule
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.services.execution import ToolExecutionService, rule_text
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository


def stack(
    tmp_path: Path, rules: tuple[PermissionRule, ...]
) -> tuple[str, KernelService, ToolExecutionService]:
    database = Database(tmp_path / "rules.db")
    operations = OperationService(OperationRepository(database))
    kernel = KernelService(operations, KernelRepository(database))
    recovery = RecoveryService(operations, kernel, RecoveryRepository(database))
    execution = ToolExecutionService(
        operations,
        kernel,
        recovery,
        ToolRunRepository(database),
        permission_rules=lambda: rules,
        permission_mode=lambda: "ask",
    )
    operation = operations.create(
        "Rules lab", OperationMode.BUG_BOUNTY, ScopeContract(includes=("127.0.0.1",))
    )
    return operation.id, kernel, execution


def test_a_deny_rule_refuses_before_any_approval(tmp_path: Path) -> None:
    operation_id, kernel, execution = stack(
        tmp_path, (PermissionRule.parse("deny shell.exec: *"),)
    )
    with pytest.raises(ToolDeniedError, match="denied by rule deny shell.exec: \\*"):
        execution.run_tool(operation_id, "shell.exec", {"command": "id"})
    assert kernel.kernel.list_approvals(operation_id) == []


def test_an_allow_rule_approves_without_a_prompt(tmp_path: Path) -> None:
    operation_id, kernel, execution = stack(
        tmp_path, (PermissionRule.parse("allow shell.exec: echo *"),)
    )
    result = execution.run_tool(operation_id, "shell.exec", {"command": "echo ruled"})
    assert result.stdout.strip() == "ruled"
    approval = kernel.kernel.get_approval(result.run.approval_id or "")
    assert approval is not None and approval.status is ApprovalStatus.APPROVED
    assert approval.decided_by.startswith("rule:allow shell.exec")


def test_a_rule_that_does_not_match_leaves_the_old_flow(tmp_path: Path) -> None:
    operation_id, _kernel, execution = stack(
        tmp_path, (PermissionRule.parse("allow shell.exec: git *"),)
    )
    with pytest.raises(ApprovalRequiredError):
        execution.run_tool(operation_id, "shell.exec", {"command": "id"})


def test_a_sandbox_tool_is_ruled_by_its_parameters(tmp_path: Path) -> None:
    operation_id, _kernel, execution = stack(
        tmp_path, (PermissionRule.parse("deny scan.nmap: *10.129.136.71*"),)
    )
    with pytest.raises(ToolDeniedError):
        execution.run_tool(operation_id, "scan.nmap", {"targets": ["10.129.136.71"]})


def test_the_rule_text_prefers_the_command_and_falls_back_to_parameters() -> None:
    assert rule_text({"command": "id", "extra": 1}) == "id"
    rendered = rule_text({"targets": ["10.0.0.1"], "_secret_env": {"X": "1"}})
    assert rendered == '{"targets": ["10.0.0.1"]}'
