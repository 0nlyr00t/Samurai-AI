"""Runs the daemon owns: one per operation, cancellable, survive client drops.

A run lives in a background task, not in the caller's connection — that is the
whole reason the daemon exists.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"


class RunBusy(RuntimeError):
    pass


@dataclass
class RunHandle:
    id: str
    operation_id: str
    route: list[str]
    task_ids: list[str] | None
    created_by: str
    state: str = STATE_RUNNING
    detail: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stop_event: threading.Event = field(default_factory=threading.Event)

    def as_frame(self) -> dict[str, Any]:
        return {
            "run_id": self.id,
            "operation_id": self.operation_id,
            "state": self.state,
            "detail": self.detail,
            "started_at": self.started_at.isoformat(),
            "task_ids": self.task_ids,
            "route": self.route,
        }


class RunRegistry:
    def __init__(
        self,
        work: Callable[[RunHandle], Awaitable[Any]],
        journal: Callable[[str, str, dict[str, Any]], None],
    ) -> None:
        self.work = work
        self.journal = journal
        self._handles: dict[str, RunHandle] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def active(self, operation_id: str) -> RunHandle | None:
        for handle in self._handles.values():
            if handle.operation_id == operation_id and handle.state == STATE_RUNNING:
                return handle
        return None

    def get(self, run_id: str) -> RunHandle:
        handle = self._handles.get(run_id)
        if handle is None:
            raise KeyError(run_id)
        return handle

    def listing(self, operation_id: str | None = None) -> list[dict[str, Any]]:
        return [
            handle.as_frame()
            for handle in self._handles.values()
            if operation_id is None or handle.operation_id == operation_id
        ]

    def start(
        self,
        operation_id: str,
        route: list[str],
        task_ids: list[str] | None,
        created_by: str,
    ) -> RunHandle:
        if self.active(operation_id) is not None:
            raise RunBusy(f"{operation_id} already has a run in flight")
        handle = RunHandle(
            id=f"run_{uuid4().hex[:10]}",
            operation_id=operation_id,
            route=route,
            task_ids=task_ids,
            created_by=created_by,
        )
        self._handles[handle.id] = handle
        self.journal(operation_id, "agent.run.start", handle.as_frame())
        self._tasks[handle.id] = asyncio.ensure_future(self._supervise(handle))
        return handle

    async def _supervise(self, handle: RunHandle) -> None:
        try:
            await self.work(handle)
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            handle.state = STATE_CANCELLED
            handle.detail = "daemon stopped"
            raise
        except Exception as exc:  # noqa: BLE001 - a run must not kill the daemon
            handle.state = STATE_FAILED
            handle.detail = str(exc)
        else:
            handle.state = STATE_DONE if not handle.stop_event.is_set() else STATE_CANCELLED
        finally:
            if handle.stop_event.is_set() and handle.state == STATE_DONE:
                handle.state = STATE_CANCELLED
                handle.detail = "cancelled by the operator"
            self.journal(handle.operation_id, "agent.run.finish", handle.as_frame())

    def cancel(self, run_id: str, reason: str = "cancelled by the operator") -> RunHandle:
        handle = self.get(run_id)
        handle.detail = reason
        handle.stop_event.set()
        return handle

    async def drain(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()


__all__ = ["RunBusy", "RunHandle", "RunRegistry"]
