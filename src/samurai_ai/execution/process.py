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
_MAX_STDIN_BYTES = 65536
#: no new output for this long means the tool is stuck (0 disables)
DEFAULT_STALL_SECONDS = 120.0
_STALL_POLL_SECONDS = 1.0


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
    #: killed because it produced nothing for a while
    stalled: bool = False


@dataclass
class _RunningProcess:
    handle: ManagedProcess
    process: asyncio.subprocess.Process
    stdout_chunks: list[bytes] = field(default_factory=list)
    stderr_chunks: list[bytes] = field(default_factory=list)
    truncated: bool = False
    last_output_at: float = field(default_factory=time.monotonic)
    stalled: bool = False


class ProcessManager:
    def __init__(
        self,
        *,
        allowed_executables: frozenset[str] = DEFAULT_ALLOWED_EXECUTABLES,
        output_limit_bytes: int = 65536,
        circuit_breaker_threshold: int = 3,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
    ) -> None:
        if circuit_breaker_threshold < 1:
            raise ValueError("circuit breaker threshold must be >= 1")
        #: an open breaker half-opens after this many seconds: a fetch
        #: timing out three times is a sick target, not a dead process host
        self.breaker_cooldown_seconds = 120.0
        self.allowed_executables = allowed_executables
        self.output_limit_bytes = output_limit_bytes
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.stall_seconds = stall_seconds
        #: how many finished results stay available for attach
        self.collected_limit = 32
        self._deadline_at: float | None = None
        self._running: dict[str, _RunningProcess] = {}
        #: finished results by handle id: status/tail/collect read, never re-run
        self._collected: dict[str, ProcessResult] = {}
        self._collected_order: list[str] = []
        self._consecutive_failures = 0
        self._breaker_opened_at: float | None = None

    def list(self) -> list[ManagedProcess]:
        return [entry.handle for entry in self._running.values()]

    def status(self, handle_id: str) -> str:
        """running | finished | unknown — a status never re-runs the command."""
        if handle_id in self._running:
            return "running"
        return "finished" if handle_id in self._collected else "unknown"

    def tail(self, handle_id: str, limit: int = 4000) -> str:
        """What the process has written so far, bounded; safe on a finished run."""
        entry = self._running.get(handle_id)
        if entry is not None:
            data = b"".join(entry.stdout_chunks + entry.stderr_chunks)
        else:
            result = self._collected.get(handle_id)
            if result is None:
                raise ProcessNotFoundError(f"process not found: {handle_id}")
            data = (result.stdout + result.stderr).encode("utf-8", errors="replace")
        text = data.decode("utf-8", errors="replace")
        return text[-limit:]

    def collect(self, handle_id: str) -> ProcessResult:
        """The finished result, kept after `wait` so an attach does not re-run."""
        result = self._collected.get(handle_id)
        if result is None:
            if handle_id in self._running:
                raise ProcessNotFoundError(
                    f"process {handle_id} is still running: wait or cancel it first"
                )
            raise ProcessNotFoundError(f"process not found: {handle_id}")
        return result

    def reset_circuit_breaker(self) -> None:
        self._consecutive_failures = 0
        self._breaker_opened_at = None

    def set_deadline(self, seconds: float | None) -> None:
        """A hard brake for every later run; ``None`` removes it.

        Per-run timeouts stay, this one caps their sum: a tool that keeps
        talking cannot outlive the deadline.
        """
        self._deadline_at = None if seconds is None else time.monotonic() + seconds

    def remaining_seconds(self) -> float | None:
        if self._deadline_at is None:
            return None
        return max(0.0, self._deadline_at - time.monotonic())

    async def start(
        self,
        argv: builtins.list[str] | tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        stdin_data: bytes | None = None,
        inherit_env: bool = True,
    ) -> ManagedProcess:
        arguments = tuple(argv)
        self._validate_argv(arguments)
        self._validate_env(env)
        self._validate_stdin(stdin_data)
        # a native tool gets exactly the declared variables, not the whole
        # operator environment: no stray token or proxy leaks into it
        if inherit_env:
            child_env = {**os.environ, **env} if env else None
        else:
            child_env = dict(env or {})
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            self._record_failure()
            raise ToolExecutionError(f"failed to spawn {arguments[0]!r}: {exc}") from exc
        if stdin_data is not None and process.stdin is not None:
            # Bounded payload: the pipe buffer holds it, so feeding it here is
            # deadlock-free and the child sees EOF immediately afterwards.
            process.stdin.write(stdin_data)
            await process.stdin.drain()
            process.stdin.close()
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
        remaining = self.remaining_seconds()
        if remaining is not None:
            timeout_seconds = (
                remaining
                if timeout_seconds is None
                else min(timeout_seconds, remaining)
            )
        readers = asyncio.gather(
            self._pump(entry.process.stdout, entry.stdout_chunks, entry),
            self._pump(entry.process.stderr, entry.stderr_chunks, entry),
        )
        watchdog = asyncio.create_task(self._watch_stall(entry))
        timed_out = False
        try:
            await asyncio.wait_for(entry.process.wait(), timeout=timeout_seconds)
            await readers
        except TimeoutError:
            timed_out = True
            readers.cancel()
            await self._terminate_group(entry.process)
        finally:
            watchdog.cancel()
            self._running.pop(handle_id, None)
        result = ProcessResult(
            exit_code=entry.process.returncode,
            stdout=b"".join(entry.stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(entry.stderr_chunks).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            truncated=entry.truncated,
            stalled=entry.stalled,
        )
        # The breaker guards the *process host*, not the exit code: `grep` with
        # no match or `test -f` on a missing file are ordinary 1s, and three of
        # them in a row used to freeze every later run for the cooldown.
        if timed_out or result.stalled:
            self._record_failure()
        else:
            self._clear_failures()
        self._remember(handle_id, result)
        return result

    async def cancel(self, handle_id: str) -> ProcessResult:
        entry = self._running.get(handle_id)
        if entry is None:
            raise ProcessNotFoundError(f"process not found: {handle_id}")
        await self._terminate_group(entry.process)
        self._running.pop(handle_id, None)
        result = ProcessResult(
            exit_code=entry.process.returncode,
            stdout=b"".join(entry.stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(entry.stderr_chunks).decode("utf-8", errors="replace"),
            cancelled=True,
            truncated=entry.truncated,
        )
        self._remember(handle_id, result)
        return result

    async def run(
        self,
        argv: builtins.list[str] | tuple[str, ...],
        timeout_seconds: float | None = None,
        *,
        env: Mapping[str, str] | None = None,
        stdin_data: bytes | None = None,
        inherit_env: bool = True,
    ) -> ProcessResult:
        handle = await self.start(
            argv, env=env, stdin_data=stdin_data, inherit_env=inherit_env
        )
        return await self.wait(handle.id, timeout_seconds=timeout_seconds)

    def _validate_argv(self, argv: tuple[str, ...]) -> None:
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            opened = self._breaker_opened_at
            if (
                opened is not None
                and time.monotonic() - opened > self.breaker_cooldown_seconds
            ):
                self.reset_circuit_breaker()
            else:
                remaining_s = 0
                if opened is not None:
                    remaining_s = max(
                        0,
                        int(
                            self.breaker_cooldown_seconds
                            - (time.monotonic() - opened)
                        ),
                    )
                raise CircuitBreakerOpenError(
                    "process circuit breaker is open after "
                    f"{self._consecutive_failures} consecutive failures "
                    "(timeouts or stalled processes — the transport, not the "
                    f"commands); retry possible in ~{remaining_s}s. Do not "
                    "retry in a loop: check the VPN/VM is alive first"
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

    def _validate_stdin(self, stdin_data: bytes | None) -> None:
        if stdin_data is None:
            return
        if len(stdin_data) > _MAX_STDIN_BYTES:
            raise ToolValidationError(
                f"stdin payload exceeds {_MAX_STDIN_BYTES} bytes"
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
            entry.last_output_at = time.monotonic()

    async def _watch_stall(self, entry: _RunningProcess) -> None:
        """Kill a tool that has gone quiet for `stall_seconds`."""
        if self.stall_seconds <= 0:
            return
        while True:
            await asyncio.sleep(_STALL_POLL_SECONDS)
            if time.monotonic() - entry.last_output_at < self.stall_seconds:
                continue
            entry.stalled = True
            await self._terminate_group(entry.process)
            return

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

    def _remember(self, handle_id: str, result: ProcessResult) -> None:
        """Keep the final result bounded in count so attach stays cheap."""
        self._collected[handle_id] = result
        self._collected_order.append(handle_id)
        while len(self._collected_order) > self.collected_limit:
            self._collected.pop(self._collected_order.pop(0), None)

    def _clear_failures(self) -> None:
        self._consecutive_failures = 0
        self._breaker_opened_at = None

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            self._breaker_opened_at = time.monotonic()
