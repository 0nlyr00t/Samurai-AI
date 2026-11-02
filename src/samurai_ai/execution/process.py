"""Async subprocess manager for managed tool execution.

Uses ``asyncio.create_subprocess_exec`` only — never a shell, never
``shell=True``. Enforces an executable allowlist, per-run timeouts, bounded
stdout/stderr capture, and a circuit breaker that opens after N consecutive
failures (no retries).
"""

from __future__ import annotations

import asyncio
import builtins
import os
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from samurai_ai.domain.execution import ToolExecutionError, ToolValidationError

DEFAULT_ALLOWED_EXECUTABLES: frozenset[str] = frozenset({"echo"})
_READ_CHUNK = 65536


class CircuitBreakerOpenError(ToolExecutionError):
    pass


class ProcessNotFoundError(ToolExecutionError):
    pass


@dataclass(frozen=True)
class ManagedProcess:
    id: str
    argv: tuple[str, ...]
    started_at: float


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False


@dataclass
class _RunningProcess:
    handle: ManagedProcess
    process: asyncio.subprocess.Process
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    truncated: bool = False


class ProcessManager:
    def __init__(
        self,
        *,
        allowed_executables: frozenset[str] = DEFAULT_ALLOWED_EXECUTABLES,
        output_limit_bytes: int = 65536,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        if circuit_breaker_threshold < 1:
            raise ValueError("circuit breaker threshold must be >= 1")
        self.allowed_executables = allowed_executables
        self.output_limit_bytes = output_limit_bytes
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self._running: dict[str, _RunningProcess] = {}
        self._consecutive_failures = 0

    def list(self) -> list[ManagedProcess]:
        return [entry.handle for entry in self._running.values()]

    def reset_circuit_breaker(self) -> None:
        self._consecutive_failures = 0

    async def start(
        self,
        argv: builtins.list[str] | tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
    ) -> ManagedProcess:
        arguments = tuple(argv)
        self._validate_argv(arguments)
        self._validate_env(env)
        child_env = {**os.environ, **env} if env else None
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            self._record_failure()
            raise ToolExecutionError(f"failed to spawn {arguments[0]!r}: {exc}") from exc
        handle = ManagedProcess(
            id=f"proc_{uuid4().hex[:12]}",
            argv=arguments,
            started_at=time.monotonic(),
        )
        self._running[handle.id] = _RunningProcess(handle=handle, process=process)
        return handle

    async def wait(self, handle_id: str, timeout_seconds: float | None = None) -> ProcessResult:
        entry = self._running.get(handle_id)
        if entry is None:
            raise ProcessNotFoundError(f"process not found: {handle_id}")
        readers = asyncio.gather(
            self._pump(entry.process.stdout, entry.stdout_chunks, entry),
            self._pump(entry.process.stderr, entry.stderr_chunks, entry),
        )
        timed_out = False
        try:
            await asyncio.wait_for(entry.process.wait(), timeout=timeout_seconds)
            await readers
        except TimeoutError:
            timed_out = True
            readers.cancel()
            await self._terminate_group(entry.process)
        finally:
            self._running.pop(handle_id, None)
        result = ProcessResult(
            exit_code=entry.process.returncode,
            stdout=b"".join(entry.stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(entry.stderr_chunks).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            truncated=entry.truncated,
        )
        if timed_out or (result.exit_code not in (0, None)):
            self._record_failure()
        elif result.exit_code == 0:
            self._consecutive_failures = 0
        return result

    async def cancel(self, handle_id: str) -> ProcessResult:
        entry = self._running.get(handle_id)
        if entry is None:
            raise ProcessNotFoundError(f"process not found: {handle_id}")
        await self._terminate_group(entry.process)
        self._running.pop(handle_id, None)
        return ProcessResult(
            exit_code=entry.process.returncode,
            stdout=b"".join(entry.stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(entry.stderr_chunks).decode("utf-8", errors="replace"),
            cancelled=True,
            truncated=entry.truncated,
        )

    async def run(
        self,
        argv: builtins.list[str] | tuple[str, ...],
        timeout_seconds: float | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        handle = await self.start(argv, env=env)
        return await self.wait(handle.id, timeout_seconds=timeout_seconds)

    def _validate_argv(self, argv: tuple[str, ...]) -> None:
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            raise CircuitBreakerOpenError(
                "process circuit breaker is open after "
                f"{self._consecutive_failures} consecutive failures"
            )
        if not argv:
            raise ToolValidationError("argv must contain at least the executable")
        executable = Path(argv[0]).name
        if executable not in self.allowed_executables:
            raise ToolValidationError(
                f"executable {argv[0]!r} is not in the allowlist "
                f"({', '.join(sorted(self.allowed_executables))})"
            )
        if any("\x00" in argument for argument in argv):
            raise ToolValidationError("argv entries cannot contain NUL bytes")

    def _validate_env(self, env: Mapping[str, str] | None) -> None:
        """The child env is additive: validate only what can break the spawn."""
        if env is None:
            return
        for name, value in env.items():
            if not name:
                raise ToolValidationError("env variable name must not be empty")
            if "\x00" in name or "\x00" in value:
                raise ToolValidationError(
                    f"env variable {name!r} cannot contain NUL bytes"
                )

    async def _pump(
        self,
        stream: asyncio.StreamReader | None,
        chunks: builtins.list[bytes],
        entry: _RunningProcess,
    ) -> None:
        if stream is None:
            return
        captured = 0
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                return
            remaining = self.output_limit_bytes - captured
            if remaining > 0:
                chunks.append(chunk[:remaining])
                captured += len(chunk[:remaining])
            if len(chunk) > remaining:
                entry.truncated = True

    async def _terminate_group(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
                pass
            await process.wait()

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
