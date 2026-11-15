"""Stateful remote sessions with explicit external outcomes.

OpenSSH ControlMaster owns the connection while individual commands use separate
channels. A timeout is not proof that the remote side did nothing: such commands
become UNKNOWN and are never replayed automatically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from samurai_ai.domain.execution import (
    ToolExecutionError,
    ToolScopeError,
    ToolValidationError,
)
from samurai_ai.domain.scope import ScopeContract
from samurai_ai.execution.native_sandbox import build_bwrap_argv, bwrap_available
from samurai_ai.execution.process import ManagedProcess, ProcessManager, ProcessResult


class SessionError(ToolExecutionError):
    pass


class SessionNotFoundError(SessionError):
    pass


class SessionRetryError(SessionError):
    pass


class SessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CommandStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExternalOutcome(StrEnum):
    OBSERVED = "observed"
    UNOBSERVED = "unobserved"


@dataclass
class SessionCommand:
    id: str
    session_id: str
    process_id: str
    command: str
    pty: bool
    status: CommandStatus = CommandStatus.RUNNING
    external_outcome: ExternalOutcome = ExternalOutcome.OBSERVED
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""

    @property
    def finished(self) -> bool:
        return self.status is not CommandStatus.RUNNING


class SessionBackend(Protocol):
    async def start(self, session: Session) -> str: ...

    async def exec(
        self, session: Session, command: str, pty: bool
    ) -> ManagedProcess: ...

    async def control(self, session: Session, operation: str) -> ProcessResult: ...


@dataclass
class Session:
    id: str
    operation_id: str
    host: str
    user: str
    port: int
    socket_path: str
    status: SessionStatus = SessionStatus.OPEN
    master_process_id: str = ""
    commands: dict[str, SessionCommand] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    key_file: str = ""
    askpass_file: str = ""
    delivered: set[str] = field(default_factory=set)
    metadata: dict[str, str] = field(default_factory=dict)
    backend: SessionBackend = field(repr=False, default=None)  # type: ignore[assignment]


def _write_private_file(path: Path, content: str, mode: int) -> None:
    path.write_text(content)
    path.chmod(mode)


class SessionService:
    def __init__(
        self,
        *,
        manager: ProcessManager | None = None,
        autonomy_required: bool = False,
        backend: SessionBackend | None = None,
    ) -> None:
        self.manager = manager or ProcessManager(
            allowed_executables=frozenset({"ssh", "bwrap"})
        )
        self.autonomy_required = autonomy_required
        self.backend = backend
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return session

    def get_command(self, session_id: str, command_id: str) -> SessionCommand:
        session = self.get(session_id)
        command = session.commands.get(command_id)
        if command is None:
            raise SessionNotFoundError(f"command not found: {command_id}")
        return command

    async def open(
        self,
        operation_id: str,
        scope: ScopeContract,
        host: str,
        user: str,
        *,
        password: str | None = None,
        key: str | None = None,
        port: int = 22,
        backend: SessionBackend | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Session:
        if self.autonomy_required and not bwrap_available():
            raise SessionError("autonomous sessions require bubblewrap")
        if password is not None and key is not None:
            raise ToolValidationError("use either password or key, not both")
        if not 1 <= port <= 65535:
            raise ToolValidationError(f"bad SSH port {port}")
        decision = scope.evaluate(host)
        if not decision.allowed:
            raise ToolScopeError(f"host {host!r} is out of scope: {decision.detail}")
        session = Session(
            id=f"sess_{uuid4().hex[:12]}",
            operation_id=operation_id,
            host=host,
            user=user,
            port=port,
            socket_path=f"/tmp/samurai-ssh-{uuid4().hex[:16]}.sock",
            backend=backend or self.backend or SshBackend(self.manager, self.autonomy_required),
            metadata=metadata or {},
        )
        try:
            env: dict[str, str] = {}
            if key is not None:
                key_path = Path(f"/tmp/samurai-ssh-{session.id}.key")
                await asyncio.to_thread(
                    _write_private_file,
                    key_path,
                    key if key.endswith("\n") else key + "\n",
                    0o600,
                )
                session.key_file = str(key_path)
            if password is not None:
                askpass = Path(f"/tmp/samurai-ssh-{session.id}-askpass.sh")
                await asyncio.to_thread(
                    _write_private_file,
                    askpass,
                    '#!/bin/sh\nprintf "%s\\n" "$SAMURAI_SSH_PASS"\n',
                    0o700,
                )
                session.askpass_file = str(askpass)
                env = {
                    "SAMURAI_SSH_PASS": password,
                    "SSH_ASKPASS": str(askpass),
                    "SSH_ASKPASS_REQUIRE": "force",
                }
            session.env = env
            self._sessions[session.id] = session
            await self._start_master(session)
            return session
        except Exception:
            await self._cleanup(session)
            raise

    async def exec(
        self,
        session_id: str,
        command: str,
        *,
        pty: bool = False,
        wait_seconds: float | None = None,
    ) -> SessionCommand:
        session = self.get(session_id)
        self._require_open(session)
        if not command.strip():
            raise ToolValidationError("command must not be empty")
        handle = await session.backend.exec(session, command, pty)
        result = SessionCommand(
            id=f"cmd_{uuid4().hex[:12]}",
            session_id=session.id,
            process_id=handle.id,
            command=command,
            pty=pty,
        )
        session.commands[result.id] = result
        if wait_seconds is not None:
            return await self.wait(session.id, result.id, timeout_seconds=wait_seconds)
        return result

    async def read(self, session_id: str, command_id: str, limit: int = 4000) -> str:
        command = self.get_command(session_id, command_id)
        return self.manager.tail(command.process_id, limit=limit)

    async def write(
        self,
        session_id: str,
        command_id: str,
        data: bytes,
        *,
        eof: bool = False,
    ) -> None:
        command = self.get_command(session_id, command_id)
        if command.status is not CommandStatus.RUNNING:
            raise SessionError(f"command {command_id} is not running")
        await self.manager.write(command.process_id, data, eof=eof)

    async def wait(
        self,
        session_id: str,
        command_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SessionCommand:
        command = self.get_command(session_id, command_id)
        if command.finished:
            return command
        result = await self.manager.wait(command.process_id, timeout_seconds=timeout_seconds)
        return self._settle(command, result)

    async def cancel(self, session_id: str, command_id: str) -> SessionCommand:
        command = self.get_command(session_id, command_id)
        if command.finished:
            return command
        result = await self.manager.cancel(command.process_id)
        return self._settle(command, result)

    async def retry(self, session_id: str, command_id: str) -> SessionCommand:
        command = self.get_command(session_id, command_id)
        if (
            command.status is CommandStatus.UNKNOWN
            or command.external_outcome is ExternalOutcome.UNOBSERVED
        ):
            raise SessionRetryError(
                "remote outcome is unknown; reconcile it before retrying"
            )
        if command.status is CommandStatus.RUNNING:
            raise SessionRetryError("command is still running")
        return await self.exec(session_id, command.command, pty=command.pty)

    async def reconnect(self, session_id: str) -> Session:
        session = self.get(session_id)
        self._require_open(session)
        check = await session.backend.control(session, "check")
        if check.exit_code == 0:
            return session
        for command in session.commands.values():
            if command.status is CommandStatus.RUNNING:
                command.status = CommandStatus.UNKNOWN
                command.external_outcome = ExternalOutcome.UNOBSERVED
        try:
            await session.backend.control(session, "exit")
        except ToolExecutionError:
            pass
        if session.master_process_id:
            try:
                await self.manager.cancel(session.master_process_id)
            except ToolExecutionError:
                pass
        await self._remove_socket(session)
        session.socket_path = f"/tmp/samurai-ssh-{uuid4().hex[:16]}.sock"
        await self._start_master(session)
        return session

    async def healthcheck(self, session_id: str) -> dict[str, Any]:
        """Prove the live lifecycle, not merely the client version."""
        session = self.get(session_id)
        self._require_open(session)
        command = await self.exec(
            session.id,
            "ver" if session.metadata.get("shell") == "cmd" else "true",
            wait_seconds=30,
        )
        if command.status is not CommandStatus.SUCCEEDED or command.exit_code != 0:
            raise SessionError(
                f"session healthcheck failed: {command.status.value}"
            )
        return {
            "status": "healthy",
            "open": True,
            "exec": True,
            "command_id": command.id,
        }

    def collect_finished(self, session_id: str) -> list[SessionCommand]:
        session = self.get(session_id)
        ready = [
            command
            for command in session.commands.values()
            if command.finished and command.id not in session.delivered
        ]
        session.delivered.update(command.id for command in ready)
        return ready

    async def close(self, session_id: str) -> None:
        session = self.get(session_id)
        if session.status is SessionStatus.CLOSED:
            return
        for command in list(session.commands.values()):
            if command.status is CommandStatus.RUNNING:
                await self.cancel(session.id, command.id)
        try:
            await session.backend.control(session, "exit")
        except ToolExecutionError:
            pass
        session.status = SessionStatus.CLOSED
        await self._cleanup(session)

    async def _start_master(self, session: Session) -> None:
        session.master_process_id = await session.backend.start(session)

    def _settle(self, command: SessionCommand, result: ProcessResult) -> SessionCommand:
        command.exit_code = result.exit_code
        command.stdout = result.stdout
        command.stderr = result.stderr
        if result.cancelled:
            command.status = CommandStatus.CANCELLED
            command.external_outcome = ExternalOutcome.UNOBSERVED
        elif result.timed_out or result.stalled or result.exit_code is None:
            command.status = CommandStatus.UNKNOWN
            command.external_outcome = ExternalOutcome.UNOBSERVED
        elif result.exit_code == 0:
            command.status = CommandStatus.SUCCEEDED
            command.external_outcome = ExternalOutcome.OBSERVED
        else:
            command.status = CommandStatus.FAILED
            command.external_outcome = ExternalOutcome.OBSERVED
        return command

    async def _cleanup(self, session: Session) -> None:
        if session.master_process_id:
            try:
                await self.manager.cancel(session.master_process_id)
            except ToolExecutionError:
                pass
        cleanup = getattr(session.backend, "cleanup", None)
        if cleanup is not None:
            await cleanup(session)
        for path in (session.key_file, session.askpass_file):
            if path:
                try:
                    await asyncio.to_thread(Path(path).unlink, missing_ok=True)
                except OSError:
                    pass
        await self._remove_socket(session)

    async def _remove_socket(self, session: Session) -> None:
        try:
            await asyncio.to_thread(Path(session.socket_path).unlink, missing_ok=True)
        except OSError:
            pass

    def _require_open(self, session: Session) -> None:
        if session.status is not SessionStatus.OPEN:
            raise SessionError(f"session {session.id} is closed")


class SshBackend:
    def __init__(self, manager: ProcessManager, autonomy_required: bool) -> None:
        self.manager = manager
        self.autonomy_required = autonomy_required

    async def start(self, session: Session) -> str:
        argv = [
            "ssh",
            "-M",
            "-S",
            session.socket_path,
            "-N",
            "-T",
            "-o",
            "ControlPersist=300",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=no" if session.askpass_file else "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(session.port),
            self._destination(session),
        ]
        if session.key_file:
            argv[1:1] = ["-i", session.key_file]
        handle = await self.manager.start(self._confine(argv), env=session.env or None)
        session.master_process_id = handle.id
        for _ in range(100):
            check = await self.control(session, "check")
            if check.exit_code == 0:
                return handle.id
            await asyncio.sleep(0.05)
        raise SessionError("SSH ControlMaster did not become ready")

    async def exec(self, session: Session, command: str, pty: bool) -> ManagedProcess:
        return await self.manager.start(
            self._confine(
                [
                    "ssh",
                    "-S",
                    session.socket_path,
                    "-tt" if pty else "-T",
                    "-o",
                    "BatchMode=no" if session.askpass_file else "BatchMode=yes",
                    self._destination(session),
                    command,
                ]
            ),
            env=session.env or None,
            keep_stdin_open=True,
        )

    async def control(self, session: Session, operation: str) -> ProcessResult:
        if operation == "check" and session.master_process_id:
            status = self.manager.status(session.master_process_id)
            if status != "running":
                return ProcessResult(exit_code=1, stdout="", stderr="")
        return await self.manager.run(
            self._confine(
                [
                    "ssh",
                    "-S",
                    session.socket_path,
                    "-O",
                    operation,
                    self._destination(session),
                ]
            )
        )

    def _confine(self, argv: list[str]) -> list[str]:
        return (
            build_bwrap_argv(argv, ("/tmp",), strict=False)
            if self.autonomy_required
            else argv
        )

    def _destination(self, session: Session) -> str:
        return f"{session.user}@{session.host}"
