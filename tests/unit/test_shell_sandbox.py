"""ShellExecTool fronts the host command with bwrap when confinement is set."""

from __future__ import annotations

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution import native_sandbox
from samurai_ai.execution.process import ProcessResult
from samurai_ai.execution.registry import ShellExecTool


class _RecordingManager:
    """Stands in for ProcessManager: records the argv it is asked to run."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.stall_seconds = 0.0

    async def run(self, argv, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.argv = list(argv)
        return ProcessResult(exit_code=0, stdout="ok", stderr="")


def _tool() -> tuple[ShellExecTool, _RecordingManager]:
    manager = _RecordingManager()
    tool = ShellExecTool(manager=manager)  # type: ignore[arg-type]
    return tool, manager


def test_sandbox_wraps_the_command_when_available(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(native_sandbox, "bwrap_available", lambda: True)
    tool, manager = _tool()
    tool.sandbox_mode = "write"
    tool.sandbox_roots = ("/home/op/work",)
    tool.invoke({"command": "mkdir /opt/evil"}, ScopeContract(includes=("10.0.0.5",)))
    assert manager.argv[0] == "bwrap"
    assert manager.argv[-3:] == ["bash", "-lc", "mkdir /opt/evil"]
    i = manager.argv.index("--bind")
    assert manager.argv[i : i + 3] == ["--bind", "/home/op/work", "/home/op/work"]


def test_sandbox_off_runs_natively(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(native_sandbox, "bwrap_available", lambda: True)
    tool, manager = _tool()
    tool.sandbox_mode = "off"
    tool.sandbox_roots = ("/home/op/work",)
    tool.invoke({"command": "id"}, ScopeContract(includes=("10.0.0.5",)))
    assert manager.argv[0] in ("bash", "sh")


def test_a_relayed_command_is_not_sandboxed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(native_sandbox, "bwrap_available", lambda: True)
    tool, manager = _tool()
    tool.relay = "prlctl exec uuid"
    tool.sandbox_mode = "write"
    tool.sandbox_roots = ("/home/op/work",)
    tool.invoke({"command": "id"}, ScopeContract(includes=("10.0.0.5",)))
    assert manager.argv[0] in ("bash", "sh")  # the far side has its own filesystem


def test_missing_bwrap_falls_back_to_native(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(native_sandbox, "bwrap_available", lambda: False)
    tool, manager = _tool()
    tool.sandbox_mode = "write"
    tool.sandbox_roots = ("/home/op/work",)
    tool.invoke({"command": "id"}, ScopeContract(includes=("10.0.0.5",)))
    assert manager.argv[0] in ("bash", "sh")
