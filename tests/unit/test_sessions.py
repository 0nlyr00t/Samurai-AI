from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.process import ProcessResult
from samurai_ai.services.sessions import (
    CommandStatus,
    ExternalOutcome,
    SessionRetryError,
    SessionService,
)


@dataclass
class _Handle:
    id: str
    argv: tuple[str, ...]


class FakeProcessManager:
    def __init__(self) -> None:
        self.started: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str] | None] = []
        self.waits: list[str] = []
        self.cancelled: list[str] = []
        self.writes: list[tuple[str, bytes, bool]] = []
        self.results: dict[str, ProcessResult] = {}
        self.run_results: list[ProcessResult] = []
        self.check_results: list[ProcessResult] = []

    async def start(self, argv, **kwargs):
        argv = tuple(argv)
        self.started.append(argv)
        self.envs.append(kwargs.get("env"))
        handle = _Handle(f"proc_{len(self.started)}", argv)
        return handle

    async def wait(self, handle_id: str, timeout_seconds: float | None = None):
        self.waits.append(handle_id)
        return self.results.get(handle_id, ProcessResult(exit_code=0, stdout="done", stderr=""))

    async def cancel(self, handle_id: str):
        self.cancelled.append(handle_id)
        return ProcessResult(exit_code=None, stdout="", stderr="", cancelled=True)

    async def run(self, argv, **kwargs):
        argv = tuple(argv)
        if "-O" in argv and "check" in argv:
            if self.check_results:
                return self.check_results.pop(0)
            return ProcessResult(exit_code=0, stdout="", stderr="")
        if self.run_results:
            return self.run_results.pop(0)
        return ProcessResult(exit_code=0, stdout="", stderr="")

    def status(self, handle_id: str) -> str:
        return "running"

    def tail(self, handle_id: str, limit: int = 4000) -> str:
        return "interactive-output"

    async def write(self, handle_id: str, data: bytes, *, eof: bool = False) -> None:
        self.writes.append((handle_id, data, eof))


def test_open_exec_read_write_wait_and_close() -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        session = await service.open(
            "op-1",
            ScopeContract(includes=("10.0.0.5",)),
            "10.0.0.5",
            "root",
            password="secret",
        )
        command = await service.exec(session.id, "msfconsole -q", pty=True)
        assert command.status is CommandStatus.RUNNING
        assert await service.read(session.id, command.id) == "interactive-output"
        await service.write(session.id, command.id, b"help\n")
        assert manager.writes == [(command.process_id, b"help\n", False)]
        result = await service.wait(session.id, command.id)
        assert result.status is CommandStatus.SUCCEEDED
        assert result.external_outcome is ExternalOutcome.OBSERVED
        assert service.collect_finished(session.id) == [result]
        assert service.collect_finished(session.id) == []
        await service.close(session.id)
        assert service.get(session.id).status.value == "closed"
        assert all("secret" not in " ".join(argv) for argv in manager.started)
        assert any("-tt" in argv for argv in manager.started)

    asyncio.run(exercise())


def test_timeout_is_unknown_and_retry_is_refused() -> None:
    manager = FakeProcessManager()
    manager.results["proc_2"] = ProcessResult(
        exit_code=None, stdout="partial", stderr="", timed_out=True
    )
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        session = await service.open(
            "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.5", "root"
        )
        command = await service.exec(session.id, "id")
        result = await service.wait(session.id, command.id, timeout_seconds=0.01)
        assert result.status is CommandStatus.UNKNOWN
        assert result.external_outcome is ExternalOutcome.UNOBSERVED
        with pytest.raises(SessionRetryError):
            await service.retry(session.id, command.id)
        assert service.collect_finished(session.id) == [result]
        assert service.collect_finished(session.id) == []

    asyncio.run(exercise())


def test_cancel_is_unobserved_and_retry_is_refused() -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        session = await service.open(
            "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.5", "root"
        )
        command = await service.exec(session.id, "long-running")
        result = await service.cancel(session.id, command.id)
        assert result.status is CommandStatus.CANCELLED
        assert result.external_outcome is ExternalOutcome.UNOBSERVED
        with pytest.raises(SessionRetryError):
            await service.retry(session.id, command.id)

    asyncio.run(exercise())


def test_reconnect_marks_running_unknown_without_replay() -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        session = await service.open(
            "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.5", "root"
        )
        command = await service.exec(session.id, "id")
        manager.check_results = [
            ProcessResult(exit_code=1, stdout="", stderr=""),
            ProcessResult(exit_code=0, stdout="", stderr=""),
        ]
        await service.reconnect(session.id)
        assert service.get_command(session.id, command.id).status is CommandStatus.UNKNOWN
        assert len(manager.started) == 3  # master, exec, replacement master; no replay
        await service.exec(session.id, "whoami")
        assert len(manager.started) == 4

    asyncio.run(exercise())


def test_scope_is_checked_before_spawn() -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        with pytest.raises(Exception, match="out of scope"):
            await service.open(
                "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.6", "root"
            )
        assert manager.started == []

    asyncio.run(exercise())


def test_session_control_tool_exposes_the_same_lifecycle() -> None:
    from samurai_ai.execution.session_tools import SessionControlTool

    manager = FakeProcessManager()
    manager.results["proc_2"] = ProcessResult(
        exit_code=None, stdout="partial", stderr="", timed_out=True
    )
    tool = SessionControlTool(manager=manager)  # type: ignore[arg-type]
    scope = ScopeContract(includes=("10.0.0.5",))
    opened = tool.invoke(
        {
            "action": "open",
            "host": "10.0.0.5",
            "user": "root",
            "_operation_id": "op-1",
        },
        scope,
    )
    session_id = json.loads(opened.stdout)["session_id"]
    started = tool.invoke(
        {"action": "exec", "session_id": session_id, "command": "id"},
        scope,
    )
    command_id = json.loads(started.stdout)["command_id"]
    result = tool.invoke(
        {"action": "wait", "session_id": session_id, "command_id": command_id},
        scope,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "unknown"
    assert payload["external_outcome"] == "unobserved"
    assert payload["retry_allowed"] is False
    assert result.unknown is True
    assert tool.sessions.get(session_id).operation_id == "op-1"


def test_healthcheck_proves_open_and_exec() -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager)  # type: ignore[arg-type]

    async def exercise() -> None:
        session = await service.open(
            "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.5", "root"
        )
        result = await service.healthcheck(session.id)
        assert result["status"] == "healthy"
        assert result["open"] is True and result["exec"] is True
        manager.results["proc_3"] = ProcessResult(exit_code=1, stdout="", stderr="bad")
        with pytest.raises(Exception, match="healthcheck failed"):
            await service.healthcheck(session.id)

    asyncio.run(exercise())


def test_session_recipe_provisions_openssh_without_replacing_the_adapter() -> None:
    from samurai_ai.services.catalog import RecipeCatalog

    recipe = RecipeCatalog().get("session.control")
    assert recipe is not None
    assert recipe.executable == "ssh"
    assert recipe.execution.native_preferred is False
    assert recipe.provision.healthcheck == ("-V",)


def test_session_recipe_builds_a_provisioning_plan() -> None:
    from samurai_ai.domain.environment import EnvironmentProfile, OsFamily
    from samurai_ai.services.provisioner import Provisioner

    profile = EnvironmentProfile.model_validate(
        {
            "os_family": "linux",
            "distro": "debian",
            "arch": "x86_64",
            "package_managers": {"apt-get": "/usr/bin/apt-get"},
            "sudo_noninteractive": True,
        }
    )
    assert profile.os_family is OsFamily.LINUX
    plan = Provisioner(operations=None, kernel=None).plan("session.control", profile)
    assert plan.package == "openssh-client"
    assert plan.healthcheck == ("ssh", "-V")


def test_recipe_loader_ignores_appledouble_files(tmp_path: Path) -> None:
    from samurai_ai.assets import assets_root
    from samurai_ai.services.catalog import RecipeCatalog

    (tmp_path / "._nmap.json").write_bytes(b"\xa3not-json")
    (tmp_path / "session.control.json").write_text(
        (assets_root("recipes") / "session.control.json").read_text()
    )
    catalog = RecipeCatalog(directory=tmp_path, extra=())
    assert catalog.get("session.control") is not None


def test_production_confinement_keeps_the_control_root() -> None:
    from samurai_ai.services.sessions import SshBackend

    backend = SshBackend(FakeProcessManager(), autonomy_required=True)  # type: ignore[arg-type]
    argv = backend._confine(["ssh"])
    assert "--bind" in argv
    assert "/tmp" in argv
    assert argv[-1] == "ssh"


def test_unattended_production_requires_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = FakeProcessManager()
    service = SessionService(manager=manager, autonomy_required=True)  # type: ignore[arg-type]
    monkeypatch.setattr("samurai_ai.services.sessions.bwrap_available", lambda: False)

    async def exercise() -> None:
        with pytest.raises(Exception, match="bubblewrap"):
            await service.open(
                "op-1", ScopeContract(includes=("10.0.0.5",)), "10.0.0.5", "root"
            )

    asyncio.run(exercise())
