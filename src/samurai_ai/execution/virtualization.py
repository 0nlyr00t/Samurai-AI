"""Virtual machine control through the host hypervisor binaries.

Hypervisors keep their state on a daemon or on disk of the host, so a container
cannot reach them: these tools run the host binary directly (no network) with a
closed argv built by the adapter. Only the binaries the operator has installed
are usable — a missing one fails with a clear message instead of a spawn error.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Any

from samurai_ai.domain.execution import (
    NetworkAccess,
    ToolDefinition,
    ToolKind,
    ToolValidationError,
)
from samurai_ai.domain.kernel import RiskClass
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessManager, ProcessResult
from samurai_ai.execution.registry import ToolOutput

_VM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
_PATH = re.compile(r"[^\s\x00]{1,512}\Z")


class _HostTool:
    """A tool that shells out to a host binary without a container."""

    executables: tuple[str, ...] = ()

    def __init__(self, manager: ProcessManager | None = None) -> None:
        self.manager = manager or ProcessManager(
            allowed_executables=frozenset(self.executables) | {"echo"}
        )

    @property
    def definition(self) -> ToolDefinition:
        raise NotImplementedError

    def _run(self, argv: list[str], timeout: float = 120.0) -> ToolOutput:
        result = asyncio.run(self.manager.run(argv, timeout_seconds=timeout))
        return self._output(result)

    @staticmethod
    def _output(result: ProcessResult) -> ToolOutput:
        stderr = result.stderr
        if result.timed_out:
            stderr += "\n[host command timed out]"
        return ToolOutput(
            stdout=result.stdout,
            stderr=stderr,
            exit_code=result.exit_code if result.exit_code is not None else -1,
            timed_out=result.timed_out,
        )

    @staticmethod
    def _require(executable: str) -> None:
        if shutil.which(executable) is None:
            raise ToolValidationError(
                f"{executable} is not installed on this host"
            )


class VmControlTool(_HostTool):
    """List, start, stop and snapshot VMs on a local hypervisor."""

    executables = ("VBoxManage", "virsh")

    def __init__(self, manager: ProcessManager | None = None) -> None:
        super().__init__(manager)
        self._definition = ToolDefinition(
            id="vm.control",
            version=1,
            kind=ToolKind.PROCESS,
            description="Control local virtual machines: list, start, stop, reset "
            "and snapshots (VirtualBox or libvirt)",
            risk_class=RiskClass.R2,
            network=NetworkAccess.NONE,
            timeout_seconds=180,
            output_limit_bytes=262144,
            parameters={
                "type": "object",
                "properties": {
                    "backend": {
                        "type": "string",
                        "enum": ["vbox", "virsh"],
                        "default": "vbox",
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "start",
                            "stop",
                            "reset",
                            "snapshot-list",
                            "snapshot-take",
                            "snapshot-restore",
                        ],
                        "default": "list",
                    },
                    "name": {"type": "string", "maxLength": 64},
                    "snapshot": {"type": "string", "maxLength": 64},
                },
                "required": ["backend"],
                "additionalProperties": False,
            },
            capabilities=("vm", "hypervisor"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_args(self, params: dict[str, Any]) -> list[str]:
        backend = str(params.get("backend", "vbox"))
        action = str(params.get("action", "list"))
        name = str(params.get("name") or "")
        snapshot = str(params.get("snapshot") or "")
        if action != "list" and not name:
            raise ToolValidationError(f"{self.definition.id}: action {action} needs a name")
        if name and not _VM_NAME.fullmatch(name):
            raise ToolValidationError(f"{self.definition.id}: bad vm name {name!r}")
        if backend == "vbox":
            self._require("VBoxManage")
            table = {
                "list": ["VBoxManage", "list", "vms"],
                "start": ["VBoxManage", "startvm", name, "--type", "headless"],
                "stop": ["VBoxManage", "controlvm", name, "acpipowerbutton"],
                "reset": ["VBoxManage", "controlvm", name, "reset"],
                "snapshot-list": ["VBoxManage", "snapshot", name, "list"],
                "snapshot-take": ["VBoxManage", "snapshot", name, "take", snapshot],
                "snapshot-restore": ["VBoxManage", "snapshot", name, "restore", snapshot],
            }
        elif backend == "virsh":
            self._require("virsh")
            table = {
                "list": ["virsh", "list", "--all"],
                "start": ["virsh", "start", name],
                "stop": ["virsh", "shutdown", name],
                "reset": ["virsh", "reset", name],
                "snapshot-list": ["virsh", "snapshot-list", name],
                "snapshot-take": ["virsh", "snapshot-create-as", name, snapshot],
                "snapshot-restore": ["virsh", "snapshot-revert", name, snapshot],
            }
        else:
            raise ToolValidationError(f"{self.definition.id}: unknown backend {backend!r}")
        argv = table[action]
        if action.startswith("snapshot") and not snapshot:
            raise ToolValidationError(
                f"{self.definition.id}: action {action} needs a snapshot name"
            )
        return argv

    def preflight(self, params: dict[str, Any]) -> None:
        self.build_args(params)

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        return self._run(self.build_args(params))


class VmDiskTool(_HostTool):
    """Inspect and convert disk images with qemu-img (offline, no hypervisor)."""

    executables = ("qemu-img",)

    def __init__(self, manager: ProcessManager | None = None) -> None:
        super().__init__(manager)
        self._definition = ToolDefinition(
            id="vm.disk",
            version=1,
            kind=ToolKind.PROCESS,
            description="Inspect or convert a local disk image with qemu-img "
            "(info, check, convert)",
            risk_class=RiskClass.R2,
            network=NetworkAccess.NONE,
            timeout_seconds=600,
            output_limit_bytes=131072,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["info", "check", "convert"],
                        "default": "info",
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": 512},
                    "target": {"type": "string", "maxLength": 512},
                    "format": {
                        "type": "string",
                        "enum": ["qcow2", "raw", "vmdk", "vdi"],
                        "default": "qcow2",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            capabilities=("vm", "disk"),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    def build_args(self, params: dict[str, Any]) -> list[str]:
        self._require("qemu-img")
        action = str(params.get("action", "info"))
        path = str(params["path"])
        if not _PATH.fullmatch(path):
            raise ToolValidationError(f"{self.definition.id}: bad path {path!r}")
        if action == "info":
            return ["qemu-img", "info", "--output=json", path]
        if action == "check":
            return ["qemu-img", "check", path]
        target = str(params.get("target") or "")
        if not target:
            raise ToolValidationError("convert needs a target path")
        if not _PATH.fullmatch(target):
            raise ToolValidationError(f"{self.definition.id}: bad target {target!r}")
        fmt = str(params.get("format", "qcow2"))
        return ["qemu-img", "convert", "-O", fmt, path, target]

    def preflight(self, params: dict[str, Any]) -> None:
        self.build_args(params)

    def invoke(self, params: dict[str, Any], scope: ScopeContract) -> ToolOutput:
        return self._run(self.build_args(params), timeout=600.0)


__all__ = ["VmControlTool", "VmDiskTool"]
