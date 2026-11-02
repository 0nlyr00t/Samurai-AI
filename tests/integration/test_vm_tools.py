"""VM tools: closed argv over host binaries, no container involved."""

import shutil
from pathlib import Path

import pytest

from samurai_ai.domain.execution import ToolValidationError
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.operations import OperationMode
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import ToolRegistry
from samurai_ai.execution.virtualization import VmControlTool, VmDiskTool
from samurai_ai.services.execution import ToolExecutionService
from samurai_ai.services.kernel import KernelService
from samurai_ai.services.operations import OperationService
from samurai_ai.services.recovery import RecoveryService
from samurai_ai.storage.database import Database
from samurai_ai.storage.execution import ToolRunRepository
from samurai_ai.storage.kernel import KernelRepository
from samurai_ai.storage.operations import OperationRepository
from samurai_ai.storage.recovery import RecoveryRepository


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(argv))
        return ProcessResult(exit_code=0, stdout="ran", stderr="")


def make_stack(
    tmp_path: Path, adapter: object
) -> tuple[OperationService, ToolExecutionService, FakeManager]:
    manager = FakeManager()
    database = Database(tmp_path / "vm.db")
    service = OperationService(OperationRepository(database))
    kernel = KernelService(service, KernelRepository(database))
    recovery = RecoveryService(service, kernel, RecoveryRepository(database))
    registry = ToolRegistry(adapters=(adapter(manager),))  # type: ignore[operator]
    execution = ToolExecutionService(
        service, kernel, recovery, ToolRunRepository(database), registry=registry
    )
    return service, execution, manager


def make_operation(service: OperationService) -> str:
    operation = service.create(
        "vm lab", OperationMode.CTF_HTB, ScopeContract(includes=("example.com",))
    )
    return operation.id


def run(execution, operation_id: str, tool_id: str, params: dict):
    approval = execution.kernel.request_approval(
        operation_id, f"tool.run {tool_id}", RiskClass.R2
    )
    execution.kernel.approve(approval.id, decided_by="operator")
    return execution.run_tool(operation_id, tool_id, params, approval_id=approval.id)


def test_vm_control_builds_a_closed_argv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    service, execution, manager = make_stack(tmp_path, VmControlTool)
    operation_id = make_operation(service)
    run(execution, operation_id, "vm.control", {"backend": "vbox", "action": "list"})
    assert manager.calls[-1] == ("VBoxManage", "list", "vms")

    run(
        execution,
        operation_id,
        "vm.control",
        {"backend": "virsh", "action": "snapshot-take", "name": "win10", "snapshot": "pre"},
    )
    assert manager.calls[-1] == ("virsh", "snapshot-create-as", "win10", "pre")


def test_vm_control_validates_names_and_required_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    service, execution, _ = make_stack(tmp_path, VmControlTool)
    operation_id = make_operation(service)
    with pytest.raises(ToolValidationError, match="needs a name"):
        run(execution, operation_id, "vm.control", {"backend": "vbox", "action": "start"})
    with pytest.raises(ToolValidationError, match="bad vm name"):
        run(
            execution,
            operation_id,
            "vm.control",
            {"backend": "vbox", "action": "start", "name": "bad;name"},
        )


def test_vm_control_says_what_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    service, execution, _ = make_stack(tmp_path, VmControlTool)
    operation_id = make_operation(service)
    with pytest.raises(ToolValidationError, match="not installed"):
        run(execution, operation_id, "vm.control", {"backend": "vbox", "action": "list"})


def test_vm_disk_uses_json_info_and_converts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    service, execution, manager = make_stack(tmp_path, VmDiskTool)
    operation_id = make_operation(service)
    run(execution, operation_id, "vm.disk", {"path": "/tmp/a.qcow2"})
    assert manager.calls[-1] == ("qemu-img", "info", "--output=json", "/tmp/a.qcow2")
    run(
        execution,
        operation_id,
        "vm.disk",
        {
            "action": "convert",
            "path": "/tmp/a.qcow2",
            "target": "/tmp/a.raw",
            "format": "raw",
        },
    )
    assert manager.calls[-1] == (
        "qemu-img",
        "convert",
        "-O",
        "raw",
        "/tmp/a.qcow2",
        "/tmp/a.raw",
    )
